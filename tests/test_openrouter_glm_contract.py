"""Deterministic wire-contract tests for the reviewed OpenRouter GLM model."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterModelProfile, OpenRouterProvider

from candidate_screening.ai.interpreter import AIProviderError, InterpreterDependencies
from candidate_screening.ai.pydantic_ai import PydanticAIInterpreter
from candidate_screening.ai.schemas import TurnInterpretation
from candidate_screening.config import Settings
from candidate_screening.domain.enums import Language
from candidate_screening.domain.models import ScreeningState

GLM_MODEL = "z-ai/glm-5.2:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "llm_model": f"openrouter:{GLM_MODEL}",
        "openrouter_api_key": SecretStr("test-openrouter"),
        "openrouter_data_collection": "deny",
        "openrouter_zdr": True,
        "openrouter_require_parameters": True,
        "openrouter_reasoning_effort": "high",
        "openrouter_output_retries": 1,
        "openrouter_timeout_seconds": 60,
        "llm_max_retries": 1,
        "llm_max_output_tokens": 800,
        "llm_extraction_temperature": 0.0,
    }
    values.update(overrides)
    return Settings(**values)  # pyright: ignore[reportCallIssue]


def _dependencies() -> InterpreterDependencies:
    return InterpreterDependencies(
        state=ScreeningState.empty(Language.ES),
        language=Language.ES,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        local_date="2026-01-02",
    )


def _completion_response(
    content: str,
    *,
    status_code: int = 200,
    reasoning: str | None = None,
) -> httpx.Response:
    if status_code != 200:
        body: dict[str, Any] = {
            "error": {"code": status_code, "message": "provider details must not escape"}
        }
    else:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning"] = reasoning
        body = {
            "id": "mock-completion",
            "object": "chat.completion",
            "created": 1,
            "model": GLM_MODEL,
            "provider": "Decart FP4",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 17,
                "completion_tokens": 11,
                "total_tokens": 28,
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        }
    return httpx.Response(status_code, json=body)


def _native_model(
    handler: Any,
) -> tuple[OpenRouterModel, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=OPENROUTER_URL)
    openai_client = AsyncOpenAI(
        api_key="test-openrouter",
        base_url=OPENROUTER_URL,
        http_client=cast(Any, http_client),
        max_retries=0,
    )
    # The OpenAI SDK performs a platform probe in a worker thread on the first
    # request.  Setting this deterministic, non-secret value avoids a Python
    # 3.14/thread-runtime dependency in the MockTransport contract tests.
    openai_client._platform = "Linux"  # pyright: ignore[reportPrivateUsage]
    provider = OpenRouterProvider(openai_client=openai_client)
    provider_profile = provider.model_profile(GLM_MODEL)
    assert provider_profile is not None
    profile = OpenRouterModelProfile.from_profile(provider_profile).update(
        OpenRouterModelProfile(
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        )
    )
    return OpenRouterModel(GLM_MODEL, provider=provider, profile=profile), http_client


def _factory(model: OpenRouterModel) -> Any:
    class Factory:
        def create(self, _settings: Settings) -> OpenRouterModel:
            return model

    return Factory()


def _request_body(request: httpx.Request) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(request.content))


def _schema_nodes(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        yield cast(dict[str, Any], mapping)
        for child in mapping.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            yield from _schema_nodes(child)


def _assert_native_glm_request(body: dict[str, Any]) -> None:
    assert body["model"] == GLM_MODEL
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "TurnInterpretation"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    for node in _schema_nodes(schema):
        if node.get("type") != "object" or "properties" not in node:
            continue
        properties = cast(dict[str, Any], node["properties"])
        assert node.get("additionalProperties") is False
        assert set(cast(list[str], node.get("required", []))) == set(properties)
    assert "tools" not in body
    assert "tool_choice" not in body
    assert body["reasoning"] == {"enabled": True, "effort": "high", "exclude": True}
    assert body["provider"] == {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }
    assert body["usage"] == {"include": True}
    assert body["temperature"] == 0.0
    # OpenAI/Pydantic AI maps the generic model setting to the current
    # ``max_completion_tokens`` wire spelling.
    assert body.get("max_completion_tokens", body.get("max_tokens")) == 4_000


@pytest.mark.asyncio
async def test_glm_native_wire_contract_returns_typed_output_and_usage() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(_request_body(request))
        output = TurnInterpretation.model_validate(
            {
                "detected_language": "es",
                "language_confidence": 1,
                "intent": "answer",
                "full_name": {
                    "value": "Laura García",
                    "provided": True,
                    "evidence": "Me llamo Laura García",
                },
            }
        ).model_dump(mode="json")
        return _completion_response(json.dumps(output, ensure_ascii=False), reasoning="private")

    model, http_client = _native_model(handler)
    try:
        interpreter = PydanticAIInterpreter(_settings(), model_factory=_factory(model))
        with override_allow_model_requests(True):
            result = await interpreter.interpret("Me llamo Laura García.", _dependencies())

        assert result.interpretation.full_name is not None
        assert result.interpretation.full_name.value == "Laura García"
        assert result.interpretation.full_name.provided is True
        assert result.usage["requests"] == 1
        assert len(requests) == 1
        _assert_native_glm_request(requests[0])
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_glm_retries_one_invalid_native_response_and_keeps_wire_native() -> None:
    requests: list[dict[str, Any]] = []
    valid_output = TurnInterpretation().model_dump(mode="json")
    responses = iter(["not-json", json.dumps(valid_output)])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(_request_body(request))
        return _completion_response(next(responses))

    model, http_client = _native_model(handler)
    try:
        interpreter = PydanticAIInterpreter(
            _settings(llm_max_retries=5, openrouter_output_retries=1),
            model_factory=_factory(model),
        )
        with override_allow_model_requests(True):
            result = await interpreter.interpret("Sí", _dependencies())

        assert result.interpretation.intent.value == "answer"
        assert len(requests) == 2
        for body in requests:
            _assert_native_glm_request(body)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_glm_zero_output_retry_does_not_repeat_invalid_native_response() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(_request_body(request))
        return _completion_response("not-json")

    model, http_client = _native_model(handler)
    try:
        interpreter = PydanticAIInterpreter(
            _settings(llm_max_retries=5, openrouter_output_retries=0),
            model_factory=_factory(model),
        )
        with override_allow_model_requests(True), pytest.raises(AIProviderError) as error:
            await interpreter.interpret("Sí", _dependencies())

        assert error.value.category == "invalid_output"
        assert str(error.value) == "model provider request failed"
        assert len(requests) == 1
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_glm_rate_limit_is_normalized_without_provider_details_or_retry() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(_request_body(request))
        return _completion_response("unused", status_code=429)

    model, http_client = _native_model(handler)
    try:
        interpreter = PydanticAIInterpreter(
            _settings(llm_max_retries=5, openrouter_output_retries=1),
            model_factory=_factory(model),
        )
        with override_allow_model_requests(True), pytest.raises(AIProviderError) as error:
            await interpreter.interpret("Sí", _dependencies())

        assert error.value.category == "rate_limited"
        assert str(error.value) == "model provider request failed"
        assert len(requests) == 1
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_glm_timeout_is_normalized_as_timeout_without_sdk_retry() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(_request_body(request))
        raise httpx.ReadTimeout("mock timeout", request=request)

    model, http_client = _native_model(handler)
    try:
        interpreter = PydanticAIInterpreter(
            _settings(llm_max_retries=5, openrouter_output_retries=1),
            model_factory=_factory(model),
        )
        with override_allow_model_requests(True), pytest.raises(AIProviderError) as error:
            await interpreter.interpret("Sí", _dependencies())

        assert error.value.category == "timeout"
        assert str(error.value) == "model provider request failed"
        assert len(requests) == 1
    finally:
        await http_client.aclose()
