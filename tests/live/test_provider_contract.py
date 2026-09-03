"""Opt-in smoke coverage for the configured native provider contract."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from pydantic_ai.models import override_allow_model_requests

from candidate_screening.ai.interpreter import InterpreterDependencies
from candidate_screening.ai.pydantic_ai import PydanticAIInterpreter, PydanticAIModelFactory
from candidate_screening.config import Settings
from candidate_screening.domain.enums import Language
from candidate_screening.domain.models import ScreeningState

pytestmark = pytest.mark.live


def _live_settings() -> Settings:
    if os.getenv("RUN_LIVE_PROVIDER_CONTRACT") != "1":
        pytest.skip("set RUN_LIVE_PROVIDER_CONTRACT=1 to opt into external provider requests")
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model=os.getenv("LLM_MODEL", "groq:openai/gpt-oss-20b"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
        groq_api_key=os.getenv("GROQ_API_KEY"),
        openrouter_data_collection=os.getenv("OPENROUTER_DATA_COLLECTION", "deny"),
        openrouter_zdr=os.getenv("OPENROUTER_ZDR", "true"),
        openrouter_require_parameters=os.getenv("OPENROUTER_REQUIRE_PARAMETERS", "false"),
        llm_base_url=os.getenv("LLM_BASE_URL"),
    )
    if not settings.has_selected_provider_credentials():
        pytest.skip(f"{settings.selected_provider_key_variable} is required for the live contract")
    return settings.model_copy(
        update={
            "llm_timeout_seconds": 30,
            "llm_max_retries": 0,
            "llm_max_output_tokens": 800,
        }
    )


@pytest.mark.asyncio
async def test_selected_native_provider_returns_typed_interpretation() -> None:
    settings = _live_settings()
    model = PydanticAIModelFactory().create(settings)
    assert model.model_name == settings.llm_model_name
    assert model.system == settings.llm_provider

    interpreter = PydanticAIInterpreter(settings, model=model)
    dependencies = InterpreterDependencies(
        state=ScreeningState.empty(Language.ES),
        language=Language.ES,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        local_date="2026-01-02",
    )
    with override_allow_model_requests(True):
        result = await interpreter.interpret(
            "Me llamo Laura García, tengo carnet y vivo en Madrid.",
            dependencies,
        )

    assert result.interpretation.full_name is not None
    assert result.interpretation.full_name.value == "Laura García"
    assert result.interpretation.full_name.provided is True
    assert result.interpretation.drivers_license is not None
    assert result.interpretation.drivers_license.value is True
    assert result.interpretation.drivers_license.provided is True
    assert result.interpretation.location is not None
    assert result.interpretation.location.provided is True
    assert result.interpretation.location.city == "Madrid"
    assert result.usage.get("requests", 0) >= 1
