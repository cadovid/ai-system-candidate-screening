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


def test_openrouter_model_selection_preserves_model_identifier() -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="openrouter:z-ai/glm-5.2:free",
        openai_api_key=None,
        openrouter_api_key=SecretStr("openrouter-secret"),
    )

    assert settings.llm_provider == "openrouter"
    assert settings.llm_model_name == "z-ai/glm-5.2:free"
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
    assert settings.groq_transport_retries == 2
    assert settings.groq_output_retries == 1
    assert settings.groq_reasoning_effort == "low"
    assert settings.llm_extraction_temperature == 0.0


def test_groq_reasoning_effort_is_configurable() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("groq-secret"),
        groq_reasoning_effort="high",
    )

    assert settings.groq_reasoning_effort == "high"


def test_groq_retry_budgets_and_extraction_temperature_are_configurable() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-20b",
        groq_api_key=SecretStr("groq-secret"),
        groq_transport_retries=1,
        groq_output_retries=0,
        llm_extraction_temperature=0.2,
    )

    assert settings.groq_transport_retries == 1
    assert settings.groq_output_retries == 0
    assert settings.llm_extraction_temperature == 0.2


def test_invalid_groq_reasoning_effort_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            llm_model="groq:openai/gpt-oss-20b",
            groq_api_key=SecretStr("groq-secret"),
            groq_reasoning_effort="none",  # type: ignore[arg-type]
        )


def test_groq_retry_and_temperature_bounds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            llm_model="groq:openai/gpt-oss-20b",
            groq_api_key=SecretStr("groq-secret"),
            groq_transport_retries=3,
        )
    with pytest.raises(ValidationError):
        Settings(
            llm_model="groq:openai/gpt-oss-20b",
            groq_api_key=SecretStr("groq-secret"),
            groq_output_retries=2,
        )
    with pytest.raises(ValidationError):
        Settings(
            llm_model="groq:openai/gpt-oss-20b",
            groq_api_key=SecretStr("groq-secret"),
            llm_extraction_temperature=2.1,
        )


def test_groq_model_selection_accepts_another_valid_model_identifier() -> None:
    settings = Settings(
        llm_model="groq:openai/gpt-oss-120b",
        groq_api_key=SecretStr("groq-secret"),
        openai_api_key=None,
        openrouter_api_key=None,
    )

    assert settings.llm_provider == "groq"
    assert settings.llm_model_name == "openai/gpt-oss-120b"
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
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="openrouter:z-ai/glm-5.2:free",
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
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="openrouter:z-ai/glm-5.2:free",
        openrouter_api_key=SecretStr("openrouter-secret"),
        openrouter_require_parameters=True,
    )

    assert settings.openrouter_require_parameters is True


def test_reviewed_openrouter_model_is_the_explicit_glm_free_selector() -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="openrouter:z-ai/glm-5.2:free",
        openrouter_api_key=SecretStr("openrouter-secret"),
    )

    assert settings.llm_provider == "openrouter"
    assert settings.llm_model_name == "z-ai/glm-5.2:free"
    assert settings.openrouter_reasoning_effort == "high"
    assert settings.openrouter_output_retries == 1
    assert settings.openrouter_timeout_seconds == 60.0
    assert settings.openrouter_require_parameters is True
    assert settings.openrouter_data_collection == "deny"
    assert settings.openrouter_zdr is True


def test_openrouter_reasoning_timeout_and_retry_settings_are_configurable() -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="openrouter:z-ai/glm-5.2:free",
        openrouter_api_key=SecretStr("openrouter-secret"),
        openrouter_reasoning_effort="xhigh",
        openrouter_output_retries=0,
        openrouter_timeout_seconds=90,
    )

    assert settings.openrouter_reasoning_effort == "xhigh"
    assert settings.openrouter_output_retries == 0
    assert settings.openrouter_timeout_seconds == 90


@pytest.mark.parametrize("effort", ["none", "low", "medium"])
def test_invalid_openrouter_reasoning_effort_is_rejected(effort: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            llm_model="openrouter:z-ai/glm-5.2:free",
            openrouter_api_key=SecretStr("openrouter-secret"),
            openrouter_reasoning_effort=effort,  # type: ignore[arg-type]
        )


def test_openrouter_timeout_and_retry_bounds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            llm_model="openrouter:z-ai/glm-5.2:free",
            openrouter_api_key=SecretStr("openrouter-secret"),
            openrouter_output_retries=2,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            llm_model="openrouter:z-ai/glm-5.2:free",
            openrouter_api_key=SecretStr("openrouter-secret"),
            openrouter_timeout_seconds=4,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            llm_model="openrouter:z-ai/glm-5.2:free",
            openrouter_api_key=SecretStr("openrouter-secret"),
            openrouter_timeout_seconds=181,
        )


def test_openrouter_model_overrides_remain_accepted_without_reviewed_profile() -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        llm_model="openrouter:some/provider-model",
        openrouter_api_key=SecretStr("openrouter-secret"),
    )

    assert settings.llm_provider == "openrouter"
    assert settings.llm_model_name == "some/provider-model"
