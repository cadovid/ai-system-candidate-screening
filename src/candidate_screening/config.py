"""Typed environment configuration.

The application can be imported without a provider key (for deterministic
tests and health checks), but attempting to build a live provider client fails
with a clear configuration error.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment and an optional local .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field(default="development", validation_alias="APP_ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./var/screening.db", validation_alias="DATABASE_URL"
    )
    llm_model: str = Field(default="openai:gpt-5.6-luna", validation_alias="LLM_MODEL")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_base_url: str | None = Field(default=None, validation_alias="LLM_BASE_URL")
    llm_timeout_seconds: float = Field(
        default=15.0, ge=1.0, le=120.0, validation_alias="LLM_TIMEOUT_SECONDS"
    )
    llm_max_retries: int = Field(default=2, ge=0, le=5, validation_alias="LLM_MAX_RETRIES")
    llm_max_output_tokens: int = Field(
        default=800, ge=64, le=4_000, validation_alias="LLM_MAX_OUTPUT_TOKENS"
    )
    internal_api_key: SecretStr | None = Field(default=None, validation_alias="INTERNAL_API_KEY")
    service_areas_path: Path = Field(
        default=Path("data/service_areas/service_areas.json"), validation_alias="SERVICE_AREAS_PATH"
    )
    faq_path: Path = Field(default=Path("data/faq/faq.json"), validation_alias="FAQ_PATH")
    inactivity_hours: int = Field(default=24, ge=1, le=720, validation_alias="INACTIVITY_HOURS")
    retention_days: int = Field(default=90, ge=1, le=3_650, validation_alias="RETENTION_DAYS")
    max_input_characters: int = Field(
        default=2_000, ge=100, le=10_000, validation_alias="MAX_INPUT_CHARACTERS"
    )
    max_turns: int = Field(default=40, ge=1, le=500, validation_alias="MAX_TURNS")
    max_body_bytes: int = Field(
        default=32_000, ge=1_024, le=1_000_000, validation_alias="MAX_BODY_BYTES"
    )
    rate_limit_requests: int = Field(
        default=60, ge=1, le=10_000, validation_alias="RATE_LIMIT_REQUESTS"
    )
    rate_limit_window_seconds: float = Field(
        default=60.0, ge=1.0, le=3_600.0, validation_alias="RATE_LIMIT_WINDOW_SECONDS"
    )
    max_concurrent_turns: int = Field(
        default=16, ge=1, le=1_000, validation_alias="MAX_CONCURRENT_TURNS"
    )
    history_max_pairs: int = Field(default=6, ge=1, le=50, validation_alias="HISTORY_MAX_PAIRS")
    history_max_characters: int = Field(
        default=8_000, ge=500, le=100_000, validation_alias="HISTORY_MAX_CHARACTERS"
    )

    @field_validator("llm_model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value or ":" not in value:
            raise ValueError("LLM_MODEL must use a provider:model form, e.g. openai:gpt-5.6-luna")
        return value

    def require_openai_api_key(self) -> str:
        """Return the provider key or fail without exposing its value."""

        if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
            raise RuntimeError("OPENAI_API_KEY is required for live LLM requests")
        return self.openai_api_key.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
