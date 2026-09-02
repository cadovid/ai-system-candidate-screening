# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider

from candidate_screening.ai.interpreter import InterpreterDependencies
from candidate_screening.ai.pydantic_ai import (
    PydanticAIInterpreter,
    PydanticAIModelFactory,
    SummaryGenerator,
    _summary_output,
    _turn_interpretation,
    _usage_dict,
    build_agents,
)
from candidate_screening.ai.schemas import RecruiterSummaryOutput, TurnInterpretation
from candidate_screening.config import Settings
from candidate_screening.domain.enums import Language, ScreeningStatus
from candidate_screening.domain.models import ScreeningDecision, ScreeningState


def _dependencies(language: Language = Language.EN) -> InterpreterDependencies:
    return InterpreterDependencies(
        state=ScreeningState.empty(language),
        language=language,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        local_date="2026-01-02",
    )


def _settings() -> Settings:
    return Settings(llm_max_retries=0, llm_timeout_seconds=1)


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
    assert "full_name" in observed[0][1]
    request = observed[0][0][0]
    assert isinstance(request, ModelRequest)


@pytest.mark.asyncio
async def test_invalid_model_output_is_not_silently_coerced() -> None:
    interpreter = PydanticAIInterpreter(
        _settings(), model=TestModel(custom_output_args={"language_confidence": 2})
    )

    with pytest.raises(UnexpectedModelBehavior):
        await interpreter.interpret("not valid output", _dependencies())


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

    with pytest.raises(RuntimeError, match="provider:model"):
        PydanticAIModelFactory().create(Settings(llm_model="invalid:"))


@pytest.mark.asyncio
async def test_interpreter_rejects_empty_and_oversized_input_before_model_call() -> None:
    model = TestModel(custom_output_args={"intent": "answer"})
    interpreter = PydanticAIInterpreter(
        Settings(llm_max_retries=0, max_input_characters=100), model=model
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
