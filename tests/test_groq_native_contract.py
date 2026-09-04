# pyright: reportPrivateUsage=false

"""Deterministic contract tests for Groq GPT-OSS native structured output.

These tests deliberately use Pydantic AI's profile hook and an ``httpx`` mock
transport. They do not call Groq, and they do not encode extraction heuristics;
the behavior under test is schema/request compatibility, provider/model
selection, and the bounded native-output retry contract.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import httpx
import pytest
from groq import AsyncGroq
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.groq import GroqProvider

from candidate_screening.ai.groq_native import (
    GroqNativeModel,
    GroqNativeSchemaError,
    transform_groq_native_schema,
)
from candidate_screening.ai.interpreter import AIProviderError, InterpreterDependencies
from candidate_screening.ai.pydantic_ai import (
    PydanticAIInterpreter,
    PydanticAIModelFactory,
    _output_retry_budget,
    build_agents,
)
from candidate_screening.ai.schemas import StartAvailabilityExtraction, TurnInterpretation
from candidate_screening.config import Settings
from candidate_screening.domain.enums import Language
from candidate_screening.domain.models import ScreeningState


class _NestedAnswer(BaseModel):
    """Nested model used to force a local ``$defs``/``$ref`` graph."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(default="unknown", description="A deliberately defaulted value")
    labels: list[str] = Field(default_factory=list)


class _NestedEnvelope(BaseModel):
    """Root object with both a nested ref and an array of nested refs."""

    model_config = ConfigDict(extra="forbid")

    primary: _NestedAnswer
    alternatives: list[_NestedAnswer]


def _settings(
    *,
    provider: str = "groq",
    model: str = "openai/gpt-oss-20b",
) -> Settings:
    """Build credential-shaped settings without reading a developer .env."""

    llm_model = f"{provider}:{model}"
    if provider == "openai":
        return Settings(llm_model=llm_model, openai_api_key=SecretStr("test-openai"))
    if provider == "openrouter":
        return Settings(llm_model=llm_model, openrouter_api_key=SecretStr("test-openrouter"))
    if provider == "groq":
        return Settings(llm_model=llm_model, groq_api_key=SecretStr("test-groq"))
    raise ValueError(f"unsupported test provider: {provider}")


def _dependencies() -> InterpreterDependencies:
    return InterpreterDependencies(
        state=ScreeningState.empty(Language.ES),
        language=Language.ES,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        local_date="2026-01-02",
    )


def _schema_nodes(value: object) -> Iterator[dict[str, Any]]:
    """Yield every mapping in a JSON schema, including nested definitions."""

    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        yield cast(dict[str, Any], mapping)
        for child in mapping.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            yield from _schema_nodes(child)


def _transform_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Apply the provider transformer and convert rejection to ``None``."""

    try:
        transformed = transform_groq_native_schema(schema)
    except GroqNativeSchemaError:
        return None
    return transformed


def _assert_strict_object_shape(schema: dict[str, Any]) -> None:
    """Assert the provider-facing strict object invariants recursively."""

    for node in _schema_nodes(schema):
        if node.get("type") != "object":
            continue
        properties = node.get("properties")
        required = node.get("required")
        assert isinstance(properties, dict)
        assert isinstance(required, list)
        properties = cast(dict[str, Any], properties)
        required = cast(list[str], required)
        assert node.get("additionalProperties") is False
        assert set(required) == set(properties)


def _assert_wire_metadata_is_stripped(schema: dict[str, Any]) -> None:
    """Ensure annotations/constraints cannot leak into the Groq wire schema."""

    ignored = {
        "$schema",
        "description",
        "title",
        "default",
        "examples",
        "example",
        "deprecated",
        "readOnly",
        "writeOnly",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "contentEncoding",
        "contentMediaType",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
    for node in _schema_nodes(schema):
        assert not ignored.intersection(node)


def test_groq_native_schema_without_defs_or_refs_stays_closed_and_all_required() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }

    transformed = _transform_schema(schema)

    assert transformed is not None
    assert "$defs" not in transformed
    assert "$ref" not in transformed
    _assert_strict_object_shape(transformed)
    _assert_wire_metadata_is_stripped(transformed)


def test_groq_native_schema_inlines_nested_refs_without_mutating_original() -> None:
    original = _NestedEnvelope.model_json_schema()
    before = copy.deepcopy(original)
    assert "$defs" in original
    assert any(node.get("$ref") for node in _schema_nodes(original))

    transformed = _transform_schema(original)

    assert transformed is not None
    assert original == before
    assert all("$defs" not in node and "$ref" not in node for node in _schema_nodes(transformed))
    _assert_strict_object_shape(transformed)
    _assert_wire_metadata_is_stripped(transformed)

    # The nested object appears in the direct property and inside array items;
    # checking both prevents a shallow-only inliner from passing accidentally.
    primary = cast(dict[str, Any], transformed["properties"]["primary"])
    alternatives = cast(dict[str, Any], transformed["properties"]["alternatives"])
    assert primary.get("type") == "object"
    assert cast(dict[str, Any], alternatives["items"]).get("type") == "object"


@pytest.mark.parametrize(
    "schema",
    [
        # Missing local target.
        {
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/Missing"}},
            "required": ["value"],
            "additionalProperties": False,
            "$defs": {},
        },
        # Remote references must never trigger a network fetch or be forwarded.
        {
            "type": "object",
            "properties": {"value": {"$ref": "https://example.invalid/schema.json"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        # Cyclic refs cannot be safely inlined into Groq's strict subset.
        {
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/$defs/Node"}},
                    "required": ["next"],
                    "additionalProperties": False,
                }
            },
            "$ref": "#/$defs/Node",
        },
        # Provider-incompatible applicator/constraint keywords fail closed;
        # silently deleting them would weaken the application schema.
        {
            "type": "object",
            "properties": {"value": {"type": "string", "not": {"type": "null"}}},
            "required": ["value"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [{"type": "string"}, {"type": "integer"}],
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
    ],
    ids=[
        "unresolved-local-ref",
        "external-ref",
        "cyclic-ref",
        "unsupported-applicator",
        "unsupported-union",
    ],
)
def test_groq_native_schema_invalid_graph_or_keyword_fails_closed(
    schema: dict[str, Any],
) -> None:
    assert _transform_schema(schema) is None


@pytest.mark.parametrize(
    ("provider", "model", "native"),
    [
        ("groq", "openai/gpt-oss-20b", True),
        ("groq", "openai/gpt-oss-120b", True),
        ("groq", "openai/gpt-oss-20b-preview", False),
        ("groq", "llama-3.3-70b-versatile", False),
        ("openrouter", "openai/gpt-oss-20b", False),
        ("openai", "gpt-oss-20b", False),
    ],
)
def test_native_output_is_gated_by_provider_and_exact_model_allowlist(
    provider: str,
    model: str,
    native: bool,
) -> None:
    extraction, summary = build_agents(_settings(provider=provider, model=model))

    assert isinstance(extraction.output_type, NativeOutput) is native
    assert isinstance(summary.output_type, NativeOutput) is native
    if native:
        extraction_output = cast(NativeOutput[Any], extraction.output_type)
        summary_output = cast(NativeOutput[Any], summary.output_type)
        assert extraction_output.strict is True
        assert summary_output.strict is True


def test_allowlisted_groq_profile_advertises_native_support_and_transformer() -> None:
    model = PydanticAIModelFactory().create(_settings())

    assert isinstance(model, GroqNativeModel)
    assert model.profile.supports_json_schema_output is True


def test_non_allowlisted_groq_profile_does_not_advertise_native_transformer() -> None:
    model = PydanticAIModelFactory().create(_settings(model="llama-3.3-70b-versatile"))

    assert isinstance(model, GroqModel)
    assert model.profile.supports_json_schema_output is False


class _Capture:
    """Credential-free transport capture with an optional deterministic response sequence."""

    def __init__(
        self,
        responses: Sequence[tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.responses = tuple(responses) if responses is not None else None
        self.payload: dict[str, Any] | None = None
        self.payloads: list[dict[str, Any]] = []
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.payload = cast(dict[str, Any], json.loads(request.content))
        self.payloads.append(self.payload)
        call_index = self.calls
        self.calls += 1

        if self.responses is not None:
            if call_index >= len(self.responses):
                raise AssertionError("native transport was called beyond the configured sequence")
            status_code, body = self.responses[call_index]
            return httpx.Response(status_code, json=body, request=request)

        output = TurnInterpretation().model_dump(mode="json")
        if "response_format" in self.payload:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": json.dumps(output),
            }
        else:
            tools = cast(list[dict[str, Any]], self.payload.get("tools", []))
            function = cast(dict[str, Any], tools[0]["function"])
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-native-contract",
                        "type": "function",
                        "function": {
                            "name": function["name"],
                            "arguments": json.dumps(output),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-native-contract",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/gpt-oss-20b",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop"
                        if "response_format" in self.payload
                        else "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
            },
            request=request,
        )


def _captured_groq_agent(
    settings: Settings,
    *,
    capture: _Capture | None = None,
    retries: int = 0,
) -> tuple[_Capture, Agent[Any, Any], httpx.AsyncClient]:
    """Build a selected Groq model with a credential-free HTTP transport."""

    capture = capture or _Capture()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(capture),
        base_url="https://api.groq.com",
    )
    client = AsyncGroq(
        api_key="credential-free-test-key",
        http_client=http_client,
        max_retries=0,
        timeout=3,
    )
    # Avoid the SDK's platform probe in environments where it runs lazily.
    client._platform = "Linux"
    profile_model = PydanticAIModelFactory().create(settings)
    model_type = GroqNativeModel if isinstance(profile_model, GroqNativeModel) else GroqModel
    model = model_type(
        settings.llm_model_name,
        provider=GroqProvider(groq_client=client),
        profile=profile_model.profile,
    )
    if isinstance(model, GroqNativeModel):
        extraction = Agent(
            model=model,
            output_type=NativeOutput(TurnInterpretation, strict=True),
            deps_type=InterpreterDependencies,
            retries=retries,
        )
    else:
        extraction = Agent(
            model=model,
            output_type=TurnInterpretation,
            deps_type=InterpreterDependencies,
            retries=retries,
        )
    return capture, extraction, http_client


def _native_completion(output: dict[str, Any]) -> dict[str, Any]:
    """Build one successful native response for the mocked Groq endpoint."""

    return {
        "id": "chatcmpl-native-retry-contract",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(output)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


def _native_error(code: str) -> dict[str, Any]:
    """Build a provider-shaped error body without exposing realistic details."""

    return {
        "error": {
            "message": "deterministic native contract test failure",
            "type": "invalid_request_error",
            "code": code,
        }
    }


def _native_start_output() -> dict[str, Any]:
    """Return a valid typed interpretation carrying the Spanish ASAP phrase."""

    output = TurnInterpretation().model_dump(mode="json")
    output["start_availability"] = {
        "raw_value": "lo antes posible",
        "precision": "asap",
        "provided": True,
        "evidence": "lo antes posible",
    }
    return output


def _native_interpreter(
    settings: Settings,
    capture: _Capture,
) -> tuple[PydanticAIInterpreter, httpx.AsyncClient]:
    """Build the production native agent around a deterministic HTTP transport."""

    _, agent, http_client = _captured_groq_agent(
        settings,
        capture=capture,
        retries=_output_retry_budget(settings),
    )
    return PydanticAIInterpreter(settings, agent=agent), http_client


@pytest.mark.asyncio
async def test_native_json_validate_failed_retries_once_and_returns_typed_start_availability() -> (
    None
):
    capture = _Capture(
        responses=[
            (400, _native_error("json_validate_failed")),
            (200, _native_completion(_native_start_output())),
        ]
    )
    settings = _settings()
    interpreter, http_client = _native_interpreter(settings, capture)

    try:
        with override_allow_model_requests(True):
            result = await interpreter.interpret("lo antes posible", _dependencies())
    finally:
        await http_client.aclose()

    assert capture.calls == 2
    start = result.interpretation.start_availability
    assert isinstance(start, StartAvailabilityExtraction)
    assert start.raw_value == "lo antes posible"
    assert start.precision == "asap"
    assert start.provided is True


@pytest.mark.asyncio
async def test_native_generic_bad_request_is_unavailable_without_retry() -> None:
    capture = _Capture(responses=[(400, _native_error("invalid_request_error"))])
    interpreter, http_client = _native_interpreter(_settings(), capture)

    try:
        with override_allow_model_requests(True), pytest.raises(AIProviderError) as error:
            await interpreter.interpret("lo antes posible", _dependencies())
    finally:
        await http_client.aclose()

    assert error.value.category == "unavailable"
    assert capture.calls == 1


@pytest.mark.asyncio
async def test_native_rate_limit_is_rate_limited_without_retry() -> None:
    capture = _Capture(responses=[(429, _native_error("rate_limit_exceeded"))])
    interpreter, http_client = _native_interpreter(_settings(), capture)

    try:
        with override_allow_model_requests(True), pytest.raises(AIProviderError) as error:
            await interpreter.interpret("lo antes posible", _dependencies())
    finally:
        await http_client.aclose()

    assert error.value.category == "rate_limited"
    assert capture.calls == 1


@pytest.mark.asyncio
async def test_native_json_validate_failed_respects_zero_retry_budget() -> None:
    capture = _Capture(
        responses=[
            (400, _native_error("json_validate_failed")),
            (200, _native_completion(_native_start_output())),
        ]
    )
    settings = _settings()
    settings = settings.model_copy(update={"groq_output_retries": 0})
    interpreter, http_client = _native_interpreter(settings, capture)

    try:
        with override_allow_model_requests(True), pytest.raises(AIProviderError) as error:
            await interpreter.interpret("lo antes posible", _dependencies())
    finally:
        await http_client.aclose()

    assert error.value.category == "invalid_output"
    assert capture.calls == 1


@pytest.mark.asyncio
async def test_allowlisted_groq_request_uses_strict_native_response_format_without_tools() -> None:
    settings = _settings()
    capture, agent, http_client = _captured_groq_agent(settings)
    try:
        with override_allow_model_requests(True):
            result = await agent.run("Madrid centro", deps=_dependencies())
    finally:
        await http_client.aclose()

    assert capture.payload is not None
    payload = capture.payload
    response_format = cast(dict[str, Any], payload.get("response_format"))
    assert response_format.get("type") == "json_schema"
    json_schema = cast(dict[str, Any], response_format.get("json_schema"))
    assert json_schema.get("strict") is True
    assert json_schema.get("name")
    schema = cast(dict[str, Any], json_schema.get("schema"))
    assert all("$defs" not in node and "$ref" not in node for node in _schema_nodes(schema))
    _assert_strict_object_shape(schema)
    _assert_wire_metadata_is_stripped(schema)
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert isinstance(result.output, TurnInterpretation)


@pytest.mark.asyncio
async def test_native_schema_transform_runs_after_normal_function_tool_customization() -> None:
    settings = _settings()
    capture, agent, http_client = _captured_groq_agent(settings)

    def lookup(
        query: Annotated[str, Field(min_length=2, description="A query for the local tool")],
    ) -> str:
        return query

    agent.tool_plain(lookup)
    try:
        with override_allow_model_requests(True):
            result = await agent.run("Madrid centro", deps=_dependencies())
    finally:
        await http_client.aclose()

    assert capture.payload is not None
    payload = capture.payload
    response_format = cast(dict[str, Any], payload.get("response_format"))
    assert response_format.get("type") == "json_schema"
    native_schema = cast(dict[str, Any], response_format["json_schema"]["schema"])
    _assert_wire_metadata_is_stripped(native_schema)
    assert "tools" in payload
    tools = cast(list[dict[str, Any]], payload["tools"])
    assert len(tools) == 1
    function = cast(dict[str, Any], tools[0]["function"])
    assert function["name"] == "lookup"
    tool_schema = cast(dict[str, Any], function["parameters"])
    assert tool_schema["type"] == "object"
    assert tool_schema["required"] == ["query"]
    assert tool_schema["additionalProperties"] is False
    # ``minLength`` is a normal tool-schema constraint. It proves the Groq
    # native pass was scoped to response_format rather than replacing the
    # established function-tool customization path.
    query_schema = cast(dict[str, Any], tool_schema["properties"]["query"])
    assert query_schema["minLength"] == 2
    assert isinstance(result.output, TurnInterpretation)


@pytest.mark.asyncio
async def test_non_allowlisted_groq_request_keeps_output_tool_shape() -> None:
    settings = _settings(model="llama-3.3-70b-versatile")
    capture, agent, http_client = _captured_groq_agent(settings)
    try:
        with override_allow_model_requests(True):
            result = await agent.run("Madrid centro", deps=_dependencies())
    finally:
        await http_client.aclose()

    assert capture.payload is not None
    payload = capture.payload
    assert "response_format" not in payload
    tools = cast(list[dict[str, Any]], payload.get("tools"))
    assert tools
    assert tools[0].get("type") == "function"
    assert "tool_choice" in payload
    assert isinstance(result.output, TurnInterpretation)


@pytest.mark.asyncio
async def test_injected_fake_model_runs_native_path_without_network() -> None:
    model = TestModel(custom_output_args={"intent": "answer"})
    extraction, _ = build_agents(_settings(), model=model)

    with override_allow_model_requests(True):
        result = await PydanticAIInterpreter(_settings(), agent=extraction).interpret(
            "hello", _dependencies()
        )

    assert result.interpretation.intent.value == "answer"
    assert model.last_model_request_parameters is not None
    assert model.last_model_request_parameters.output_mode == "tool"
    assert model.last_model_request_parameters.output_tools
