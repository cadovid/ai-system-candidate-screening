from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from candidate_screening.config import Settings


def test_openai_model_selection_and_credentials() -> None:
    settings = Settings(
        llm_model="openai:gpt-test",
        openai_api_key=SecretStr("openai-secret"),
        openrouter_api_key=None,
    )

    assert settings.llm_provider == "openai"
    assert settings.llm_model_name == "gpt-test"
    assert settings.selected_provider_key_variable == "OPENAI_API_KEY"
    assert settings.has_selected_provider_credentials()
    assert settings.require_selected_provider_api_key() == "openai-secret"


def test_openrouter_model_selection_preserves_variant_suffix() -> None:
    settings = Settings(
        llm_model="openrouter:deepseek/deepseek-v4-flash:free",
        openai_api_key=None,
        openrouter_api_key=SecretStr("openrouter-secret"),
    )

    assert settings.llm_provider == "openrouter"
    assert settings.llm_model_name == "deepseek/deepseek-v4-flash:free"
    assert settings.selected_provider_key_variable == "OPENROUTER_API_KEY"
    assert settings.has_selected_provider_credentials()
    assert settings.require_selected_provider_api_key() == "openrouter-secret"


def test_only_selected_provider_key_is_required() -> None:
    settings = Settings(
        llm_model="openrouter:deepseek/deepseek-v4-flash:free",
        openai_api_key=SecretStr("unused-openai-secret"),
        openrouter_api_key=SecretStr(""),
    )

    assert not settings.has_selected_provider_credentials()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        settings.require_selected_provider_api_key()


@pytest.mark.parametrize("model", ["missing-colon", "openai:", "unknown:model"])
def test_invalid_model_selection_is_rejected(model: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "llm_model": model,
                "openai_api_key": None,
                "openrouter_api_key": None,
            }
        )


def test_invalid_openrouter_data_collection_policy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "llm_model": "openai:test-model",
                "openai_api_key": None,
                "openrouter_api_key": None,
                "openrouter_data_collection": "sometimes",
            }
        )


def test_openrouter_parameter_filter_is_configurable() -> None:
    settings = Settings(
        llm_model="openrouter:openrouter/free",
        openrouter_api_key=SecretStr("openrouter-secret"),
        openrouter_require_parameters=True,
    )

    assert settings.openrouter_require_parameters is True
