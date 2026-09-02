"""Canonical, language-independent screening state.

The models in this module deliberately contain no LLM or persistence code.
They can be validated and used to recompute a decision offline, which is an
important property for a recruitment workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_type
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    AvailabilityType,
    DisqualificationReason,
    Language,
    LocationMatchStatus,
    ReviewReason,
    SchedulePreference,
    ScreeningField,
    ScreeningStatus,
    ValidationSeverity,
)

T = TypeVar("T")


def utc_now() -> datetime:
    """Return an aware UTC timestamp for domain defaults."""

    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Base model with strict unknown-field handling at domain boundaries."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Evidence(DomainModel):
    """Minimal provenance for a model-interpreted fact."""

    message_id: str | None = Field(default=None, max_length=100)
    quote: str = Field(default="", max_length=500)


class SourcedValue[T](DomainModel):
    """A typed value accompanied by source evidence and bounded confidence."""

    value: T
    evidence: Evidence = Field(default_factory=Evidence)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    captured_at: datetime = Field(default_factory=utc_now)


class LocationState(DomainModel):
    """Candidate location and the deterministic catalogue match."""

    raw_value: str | None = Field(default=None, max_length=300)
    normalized_value: str | None = Field(default=None, max_length=300)
    service_area_id: str | None = Field(default=None, max_length=100)
    matched_name: str | None = Field(default=None, max_length=300)
    match_status: LocationMatchStatus = LocationMatchStatus.UNRESOLVED
    suggestion_ids: list[str] = Field(default_factory=list, max_length=5)
    confirmed: bool = False
    evidence: Evidence = Field(default_factory=Evidence)

    @field_validator("raw_value", "normalized_value", "service_area_id", "matched_name")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None and value.strip() else None


class DeliveryExperience(DomainModel):
    """Normalized prior delivery experience."""

    years: float = Field(ge=0.0, le=60.0)
    platforms: list[str] = Field(default_factory=list, max_length=12)
    evidence: Evidence = Field(default_factory=Evidence)

    @field_validator("platforms")
    @classmethod
    def normalize_platforms(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = " ".join(value.split()).strip()
            if normalized and normalized.casefold() not in {item.casefold() for item in result}:
                result.append(normalized[:80])
        return result


class StartDatePrecision(StrEnum):
    EXACT = "exact"
    ASAP = "asap"
    WEEK = "week"
    MONTH = "month"
    UNKNOWN = "unknown"


class StartAvailability(DomainModel):
    """A candidate's start date, preserving the original natural-language text."""

    raw_value: str = Field(min_length=1, max_length=300)
    date: date_type | None = None
    precision: StartDatePrecision = StartDatePrecision.UNKNOWN
    timezone: str | None = Field(default=None, max_length=80)
    evidence: Evidence = Field(default_factory=Evidence)

    @field_validator("raw_value")
    @classmethod
    def normalize_raw_value(cls, value: str) -> str:
        value = " ".join(value.split()).strip()
        if not value:
            raise ValueError("start availability cannot be blank")
        return value


class PendingConfirmation(DomainModel):
    """A candidate value that requires an explicit confirmation before use."""

    field: ScreeningField
    proposed_value: Any
    prompt: str = Field(default="", max_length=500)
    reason: str = Field(default="confirmation_required", max_length=100)
    created_at: datetime = Field(default_factory=utc_now)


class ScreeningState(DomainModel):
    """Canonical state persisted for one screening session."""

    full_name: SourcedValue[str] | None = None
    drivers_license: SourcedValue[bool] | None = None
    location: LocationState = Field(default_factory=LocationState)
    availability: SourcedValue[list[AvailabilityType]] | None = None
    preferred_schedule: SourcedValue[SchedulePreference] | None = None
    delivery_experience: DeliveryExperience | None = None
    start_availability: StartAvailability | None = None
    preferred_language: Language = Language.ES
    current_field: ScreeningField | None = ScreeningField.FULL_NAME
    pending_confirmation: PendingConfirmation | None = None
    clarification_counts: dict[ScreeningField, int] = Field(
        default_factory=lambda: dict[ScreeningField, int]()
    )
    disclosure_acknowledged: bool = False
    candidate_confirmed: bool = False
    ruleset_version: str = "2026-01"

    @field_validator("full_name")
    @classmethod
    def normalize_name(cls, value: SourcedValue[str] | None) -> SourcedValue[str] | None:
        if value is None:
            return None
        name = " ".join(value.value.split()).strip()
        if not name:
            raise ValueError("full name cannot be blank")
        return value.model_copy(update={"value": name[:200]})

    @field_validator("availability")
    @classmethod
    def normalize_availability(
        cls, value: SourcedValue[list[AvailabilityType]] | None
    ) -> SourcedValue[list[AvailabilityType]] | None:
        if value is None:
            return None
        unique = list(dict.fromkeys(value.value))
        if not unique:
            raise ValueError("availability must contain at least one option")
        return value.model_copy(update={"value": unique})

    def model_dump_json_safe(self) -> dict[str, Any]:
        """Serialize using JSON-compatible values for a JSON database column."""

        return self.model_dump(mode="json")

    @classmethod
    def empty(
        cls, language: Language = Language.ES, *, ruleset_version: str = "2026-01"
    ) -> ScreeningState:
        return cls(preferred_language=language, ruleset_version=ruleset_version)


class ValidationIssue(DomainModel):
    """A deterministic validation issue suitable for audit traces."""

    field: ScreeningField | None = None
    code: str = Field(max_length=100)
    message_key: str = Field(max_length=150)
    severity: ValidationSeverity = ValidationSeverity.ERROR


class ScreeningDecision(DomainModel):
    """Reproducible output of the screening engine."""

    status: ScreeningStatus
    reason_codes: list[str] = Field(default_factory=list)
    missing_fields: list[ScreeningField] = Field(default_factory=lambda: list[ScreeningField]())
    issues: list[ValidationIssue] = Field(default_factory=lambda: list[ValidationIssue]())
    ruleset_version: str = "2026-01"
    rule_trace: dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime = Field(default_factory=utc_now)

    @property
    def disqualification_reason(self) -> DisqualificationReason | None:
        for reason in DisqualificationReason:
            if reason.value in self.reason_codes:
                return reason
        return None

    @property
    def review_reason(self) -> ReviewReason | None:
        for reason in ReviewReason:
            if reason.value in self.reason_codes:
                return reason
        return None
