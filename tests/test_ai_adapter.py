# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.groq import GroqModel, GroqModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.groq import GroqProvider
from pydantic_ai.providers.openai import OpenAIProvider

from candidate_screening.ai.interpreter import AIProviderError, InterpreterDependencies
from candidate_screening.ai.pydantic_ai import (
    PydanticAIInterpreter,
    PydanticAIModelFactory,
    SummaryGenerator,
    _model_settings,
    _normalize_provider_error,
    _output_retry_budget,
    _summary_output,
    _turn_interpretation,
    _usage_dict,
    build_agents,
)
from candidate_screening.ai.schemas import RecruiterSummaryOutput, TurnInterpretation
from candidate_screening.application.reconciliation import reconcile_interpretation
from candidate_screening.config import Settings
from candidate_screening.domain.enums import Language, ScreeningField, ScreeningStatus
from candidate_screening.domain.models import ScreeningDecision, ScreeningState, StartDatePrecision


def _dependencies(language: Language = Language.EN) -> InterpreterDependencies:
    return InterpreterDependencies(
        state=ScreeningState.empty(language),
        language=language,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        local_date="2026-01-02",
    )


def _settings() -> Settings:
    # Keep adapter tests independent of a developer's private .env.  These
    # tests inject fake models, so no provider credential should be loaded.
    return Settings(
        llm_model="openai:test-model",
        openai_api_key=None,
        openrouter_api_key=None,
        llm_max_retries=0,
        llm_timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_interpreter_uses_injected_test_model_and_returns_typed_usage() -> None:
    model = TestModel(
        custom_output_args={
            "detected_language": "en",
            "language_confidence": 1,
            "intent": "answer",
            "full_name": {"value": "Ada Lovelace", "provided": True, "evidence": "Ada"},
        }
    )
    interpreter = PydanticAIInterpreter(_settings(), model=model)

    result = await interpreter.interpret("Ada Lovelace", _dependencies())

    assert result.interpretation.full_name is not None
    assert result.interpretation.full_name.value == "Ada Lovelace"
    assert result.usage["requests"] == 1
    assert result.model_name == "test"
    assert len(result.new_messages) == 3


@pytest.mark.asyncio
async def test_function_model_receives_rendered_dependencies_without_network() -> None:
    observed: list[tuple[list[object], str | None]] = []

    async def respond(messages: list[Any], info: AgentInfo) -> ModelResponse:
        observed.append((messages, info.instructions))
        assert info.output_tools
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "detected_language": "en",
                        "language_confidence": 1,
                        "intent": "question",
                        "response_requested": True,
                        "candidate_questions": ["What schedules are available?"],
                    },
                )
            ]
        )

    agent = Agent(
        FunctionModel(respond),
        output_type=TurnInterpretation,
        deps_type=InterpreterDependencies,
        instructions="test harness",
        retries=0,
    )
    interpreter = PydanticAIInterpreter(_settings(), agent=agent)
    result = await interpreter.interpret("What schedules are available?", _dependencies())

    assert result.interpretation.intent.value == "question"
    assert result.interpretation.candidate_questions == ["What schedules are available?"]
    assert len(observed) == 1
    assert observed[0][1] is not None
    assert "pending field is none" in observed[0][1]
    # Empty/default state fields are omitted to keep each extraction prompt
    # small; populated canonical values are still rendered when present.
    assert "Canonical state (context only) is:" in observed[0][1]
    assert "do not return" in observed[0][1]
    request = observed[0][0][0]
    assert isinstance(request, ModelRequest)


@pytest.mark.asyncio
async def test_actionable_relative_start_period_is_stored_as_unambiguous_patch(
    service_area_matcher: Any,
) -> None:
    observed_instructions: list[str | None] = []

    async def respond(_messages: list[Any], info: AgentInfo) -> ModelResponse:
        observed_instructions.append(info.instructions)
        assert info.output_tools
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "detected_language": "en",
                        "language_confidence": 1,
                        "intent": "answer",
                        "start_availability": {
                            "raw_value": "next week",
                            "date": None,
                            "precision": "week",
                            "provided": True,
                            "ambiguous": False,
                            "evidence": "next week",
                        },
                    },
                )
            ]
        )

    dependencies = _dependencies()
    dependencies.pending_field = ScreeningField.START_AVAILABILITY
    dependencies.state.current_field = ScreeningField.START_AVAILABILITY
    interpreter = PydanticAIInterpreter(
        _settings(),
        model=FunctionModel(respond),
    )

    result = await interpreter.interpret("next week", dependencies)
    patch = result.interpretation.start_availability

    assert patch is not None
    assert patch.provided is True
    assert patch.ambiguous is False
    assert patch.precision == "week"
    assert patch.date is None
    assert observed_instructions and observed_instructions[0] is not None
    instructions = observed_instructions[0]
    state_marker = "Canonical state (context only) is:"
    priority_marker = "Priority start-availability rule:"
    assert instructions.index(priority_marker) > instructions.index(state_marker)
    priority_tail = instructions[instructions.index(priority_marker) :]
    assert "next week" in priority_tail
    assert "next month" in priority_tail
    assert "ASAP" in priority_tail
    assert "next week" in instructions
    assert 'precision="week"' in instructions
    assert 'precision="month"' in instructions
    assert 'precision="asap"' in instructions
    assert "ambiguous=true" in instructions
    assert "sometime" in instructions

    reconciled = reconcile_interpretation(
        dependencies.state,
        result.interpretation,
        service_area_matcher=service_area_matcher,
        now=dependencies.now,
    )

    assert reconciled.state.start_availability is not None
    assert reconciled.state.start_availability.raw_value == "next week"
    assert reconciled.state.start_availability.date is None
    assert reconciled.state.start_availability.precision is StartDatePrecision.WEEK
    assert reconciled.state.pending_confirmation is None


@pytest.mark.asyncio
async def test_invalid_model_output_is_not_silently_coerced() -> None:
    interpreter = PydanticAIInterpreter(
        _settings(), model=TestModel(custom_output_args={"language_confidence": 2})
    )

    with pytest.raises(AIProviderError) as error:
        await interpreter.interpret("not valid output", _dependencies())
    assert error.value.category == "invalid_output"
    assert str(error.value) == "model provider request failed"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "language_confidence" not in repr(error.value)


@pytest.mark.asyncio
async def test_summary_generator_uses_only_validated_state_with_test_model() -> None:
    generator = SummaryGenerator(
        _settings(),
        model=TestModel(custom_output_args={"summary": "Name: Ada; result: in_progress."}),
    )

    result = await generator.generate(
        ScreeningState.empty(Language.EN),
        ScreeningDecision(status=ScreeningStatus.IN_PROGRESS),
        language=Language.EN,
    )

    assert isinstance(result, RecruiterSummaryOutput)
    assert result.summary == "Name: Ada; result: in_progress."


@pytest.mark.asyncio
async def test_summary_invalid_model_output_is_normalized_without_provider_details() -> None:
    generator = SummaryGenerator(_settings(), model=TestModel(custom_output_args={"summary": ""}))

    with pytest.raises(AIProviderError) as error:
        await generator.generate(
            ScreeningState.empty(Language.EN),
            ScreeningDecision(status=ScreeningStatus.IN_PROGRESS),
            language=Language.EN,
        )

    assert error.value.category == "invalid_output"
    assert str(error.value) == "model provider request failed"
    assert error.value.__context__ is None


@pytest.mark.asyncio
async def test_summary_function_model_can_inspect_bounded_json_payload() -> None:
    payloads: list[dict[str, Any]] = []

    async def respond(messages: list[Any], info: AgentInfo) -> ModelResponse:
        assert info.output_tools
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        part = request.parts[0]
        assert isinstance(part, UserPromptPart)
        assert isinstance(part.content, str)
        payloads.append(json.loads(part.content))
        return ModelResponse(
            parts=[
                ToolCallPart(info.output_tools[0].name, {"summary": "safe deterministic summary"})
            ]
        )

    agent = Agent(
        FunctionModel(respond),
        output_type=RecruiterSummaryOutput,
        deps_type=dict[str, Any],
        instructions="test harness",
        retries=0,
    )
    generator = SummaryGenerator(_settings(), agent=agent)
    result = await generator.generate(
        ScreeningState.empty(Language.EN),
        ScreeningDecision(status=ScreeningStatus.QUALIFIED),
        language=Language.EN,
    )

    assert result.summary == "safe deterministic summary"
    assert payloads[0]["language"] == "en"
    assert "validated_screening_state" in payloads[0]
    assert "deterministic_decision" in payloads[0]


def test_adapter_helpers_and_injected_factory_cover_compatibility_paths() -> None:
    assert _turn_interpretation({"intent": "answer"}).intent.value == "answer"
    assert _summary_output({"summary": "bounded"}).summary == "bounded"
    assert _usage_dict(lambda: {"requests": 2}) == {"requests": 2}
    assert _usage_dict(None) == {}

    class Factory:
        def __init__(self) -> None:
            self.settings: Settings | None = None

        def create(self, settings: Settings) -> Any:
            self.settings = settings
            return TestModel(custom_output_args={"intent": "answer"})

    factory = Factory()
    extraction, summary = build_agents(_settings(), model_factory=factory)
    assert factory.settings is not None
    assert extraction.name == "candidate-screening-interpreter"
    assert summary.name == "candidate-screening-summary"

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        PydanticAIModelFactory().create(
            Settings(
                llm_model="openai:test-model",
                openai_api_key=None,
                openrouter_api_key=None,
            )
        )


def test_model_factory_builds_groq_openai_and_openrouter_models() -> None:
    groq_settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("test-groq"),
        llm_timeout_seconds=23,
        llm_max_output_tokens=321,
    )
    groq = PydanticAIModelFactory().create(groq_settings)

    openai = PydanticAIModelFactory().create(
        Settings(llm_model="openai:gpt-test", openai_api_key=SecretStr("test-openai"))
    )
    openrouter_settings = Settings(
        llm_model="openrouter:openrouter/free",
        openrouter_api_key=SecretStr("test-openrouter"),
        openrouter_data_collection="deny",
        openrouter_zdr=True,
        openrouter_require_parameters=False,
    )
    openrouter = PydanticAIModelFactory().create(openrouter_settings)

    assert isinstance(groq, GroqModel)
    assert isinstance(groq._provider, GroqProvider)
    assert groq.model_name == "openai/gpt-oss-20b"
    assert groq.system == "groq"
    assert groq.profile.supports_tools is True
    assert groq.profile.default_structured_output_mode == "tool"
    assert cast(GroqModelSettings, _model_settings(groq_settings)) == {
        "max_tokens": 321,
        "timeout": 23,
        "extra_body": {
            "include_reasoning": False,
            "reasoning_effort": "low",
        },
    }

    assert isinstance(openai, OpenAIResponsesModel)
    assert openai.model_name == "gpt-test"
    assert isinstance(openrouter, OpenRouterModel)
    assert openrouter.model_name == "openrouter/free"
    assert openrouter.profile.supports_tools is True
    assert openrouter.profile.default_structured_output_mode == "tool"
    model_settings = cast(OpenRouterModelSettings, _model_settings(openrouter_settings))
    assert model_settings.get("openrouter_provider") == {
        "allow_fallbacks": False,
        "require_parameters": False,
        "data_collection": "deny",
        "zdr": True,
    }
    assert "openrouter_models" not in model_settings
    assert model_settings.get("openrouter_reasoning") == {"enabled": False}
    assert model_settings.get("openrouter_usage") == {"include": True}
    assert "openrouter_provider" not in _model_settings(
        Settings(llm_model="openai:gpt-test", openai_api_key=SecretStr("test-openai"))
    )


def test_model_factory_builds_another_supported_groq_model_without_source_changes() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-120b",
        groq_api_key=SecretStr("test-groq"),
    )

    model = PydanticAIModelFactory().create(settings)

    assert isinstance(model, GroqModel)
    assert isinstance(model._provider, GroqProvider)
    assert model.model_name == "openai/gpt-oss-120b"
    assert model.system == "groq"
    assert model.profile.supports_tools is True
    assert model.profile.default_structured_output_mode == "tool"


@pytest.mark.parametrize("model_name", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"])
def test_groq_gpt_oss_agents_keep_tool_structured_output(model_name: str) -> None:
    settings = Settings(
        llm_model=f"groq:{model_name}",
        groq_api_key=SecretStr("test-groq"),
    )

    extraction, summary = build_agents(settings, model=TestModel())

    assert extraction.output_type is TurnInterpretation
    assert summary.output_type is RecruiterSummaryOutput


def test_non_groq_agents_keep_tool_structured_output() -> None:
    extraction, summary = build_agents(_settings(), model=TestModel())

    assert extraction.output_type is TurnInterpretation
    assert summary.output_type is RecruiterSummaryOutput


def test_groq_provider_disables_hidden_sdk_http_retries() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("test-groq"),
        llm_timeout_seconds=19,
    )

    model = PydanticAIModelFactory().create(settings)
    provider = cast(GroqProvider, model._provider)

    assert provider.client.max_retries == 0
    assert provider.client.timeout == 19


def test_non_gpt_oss_groq_settings_keep_reasoning_format_compatibility() -> None:
    settings = Settings(
        llm_model="groq:llama-3.3-70b-versatile",
        groq_api_key=SecretStr("test-groq"),
    )

    assert (
        cast(GroqModelSettings, _model_settings(settings)).get("groq_reasoning_format") == "hidden"
    )
    assert "extra_body" not in _model_settings(settings)
    assert _model_settings(settings, extraction=True).get("temperature") == 0.0
    assert _model_settings(settings, extraction=False).get("temperature") is None


def test_groq_output_retry_budget_is_bounded_and_global_ceiling_still_applies() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("test-groq"),
        llm_max_retries=5,
    )
    assert settings.groq_output_retries == 1
    assert _output_retry_budget(settings) == 1

    no_global_retries = settings.model_copy(update={"llm_max_retries": 0})
    assert _output_retry_budget(no_global_retries) == 0

    no_groq_retries = settings.model_copy(update={"groq_output_retries": 0})
    assert _output_retry_budget(no_groq_retries) == 0

    extraction, summary = build_agents(settings, model=TestModel())
    assert extraction._max_output_retries == 1
    assert summary._max_output_retries == 1
    extraction_settings = cast(dict[str, Any], extraction.model_settings)
    summary_settings = cast(dict[str, Any], summary.model_settings)
    assert extraction_settings.get("temperature") == 0.0
    assert summary_settings.get("temperature") is None


@pytest.mark.asyncio
async def test_groq_retries_one_malformed_output_without_transport_retry() -> None:
    calls = 0

    async def respond(_messages: list[Any], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        assert info.output_tools
        if calls == 1:
            args: dict[str, Any] = {"language_confidence": 2}
        else:
            args = {"intent": "answer", "language_confidence": 1}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("test-groq"),
        llm_max_retries=5,
    )
    interpreter = PydanticAIInterpreter(settings, model=FunctionModel(respond))

    result = await interpreter.interpret("hello", _dependencies())

    assert result.interpretation.intent.value == "answer"
    assert calls == 2


@pytest.mark.asyncio
async def test_model_http_rate_limit_is_normalized() -> None:
    async def rate_limited(_messages: list[Any], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(429, "test-model", {"message": "not exposed"})

    agent = Agent(
        FunctionModel(rate_limited),
        output_type=TurnInterpretation,
        deps_type=InterpreterDependencies,
        retries=0,
    )
    interpreter = PydanticAIInterpreter(_settings(), agent=agent)

    with pytest.raises(AIProviderError) as error:
        await interpreter.interpret("hello", _dependencies())
    assert error.value.category == "rate_limited"
    assert str(error.value) == "model provider request failed"
    assert error.value.__cause__ is None


def test_non_http_model_api_error_is_normalized_without_provider_details() -> None:
    error = _normalize_provider_error(ModelAPIError("openrouter/free", "private provider body"))

    assert error.category == "unavailable"
    assert str(error) == "model provider request failed"
    assert "openrouter" not in str(error)


@pytest.mark.asyncio
async def test_summary_model_http_error_is_normalized() -> None:
    async def unavailable(_messages: list[Any], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(503, "test-model", {"message": "not exposed"})

    agent = Agent(
        FunctionModel(unavailable),
        output_type=RecruiterSummaryOutput,
        deps_type=dict[str, Any],
        retries=0,
    )
    generator = SummaryGenerator(_settings(), agent=agent)

    with pytest.raises(AIProviderError) as error:
        await generator.generate(
            ScreeningState.empty(Language.EN),
            ScreeningDecision(status=ScreeningStatus.QUALIFIED),
            language=Language.EN,
        )

    assert error.value.category == "unavailable"
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_interpreter_rejects_empty_and_oversized_input_before_model_call() -> None:
    model = TestModel(custom_output_args={"intent": "answer"})
    interpreter = PydanticAIInterpreter(
        Settings(
            llm_model="openai:test-model",
            openai_api_key=None,
            openrouter_api_key=None,
            llm_max_retries=0,
            max_input_characters=100,
        ),
        model=model,
    )

    with pytest.raises(ValueError, match="cannot be empty"):
        await interpreter.interpret("  ", _dependencies())
    with pytest.raises(ValueError, match="exceeds"):
        await interpreter.interpret("x" * 101, _dependencies())
    assert model.last_model_request_parameters is None


@pytest.mark.asyncio
async def test_deterministic_suite_blocks_live_provider_requests() -> None:
    model = OpenAIResponsesModel("gpt-test", provider=OpenAIProvider(api_key="test"))

    with pytest.raises(RuntimeError, match="Model requests are not allowed"):
        await model.request([], None, ModelRequestParameters())
