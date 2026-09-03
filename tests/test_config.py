from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from candidate_screening.config import Settings


def test_default_model_uses_groq_openai_gpt_oss() -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        openai_api_key=None,
        openrouter_api_key=None,
        groq_api_key=None,
    )

    assert settings.llm_model == "groq:openai/gpt-oss-20b"
    assert settings.llm_provider == "groq"
    assert settings.llm_model_name == "openai/gpt-oss-20b"
    assert settings.selected_provider_key_variable == "GROQ_API_KEY"
    assert not settings.has_selected_provider_credentials()


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


def test_groq_model_selection_and_credentials() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("groq-secret"),
        openai_api_key=None,
        openrouter_api_key=None,
    )

    assert settings.llm_provider == "groq"
    assert settings.llm_model_name == "openai/gpt-oss-20b"
    assert settings.selected_provider_key_variable == "GROQ_API_KEY"
    assert settings.has_selected_provider_credentials()
    assert settings.require_selected_provider_api_key() == "groq-secret"


@pytest.mark.parametrize("groq_api_key", [None, SecretStr("")])
def test_groq_requires_selected_key_even_when_other_keys_are_present(
    groq_api_key: SecretStr | None,
) -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="groq:openai/gpt-oss-20b",
        openai_api_key=SecretStr("unused-openai-secret"),
        openrouter_api_key=SecretStr("unused-openrouter-secret"),
        groq_api_key=groq_api_key,
    )

    assert not settings.has_selected_provider_credentials()
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        settings.require_selected_provider_api_key()


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
