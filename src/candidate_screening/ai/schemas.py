"""Structured LLM I/O schemas.

The extraction schema is deliberately a patch/intention schema, not a
screening decision schema. Model output cannot directly set a status.
"""

from __future__ import annotations

from datetime import date as date_type
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from candidate_screening.domain.enums import AvailabilityType, Language, SchedulePreference

T = TypeVar("T")


class AIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TurnIntent(StrEnum):
    ANSWER = "answer"
    QUESTION = "question"
    MIXED = "mixed"
    CORRECTION = "correction"
    OFF_TOPIC = "off_topic"
    OPT_OUT = "opt_out"
    PROMPT_INJECTION = "prompt_injection"
    UNKNOWN = "unknown"


class ExtractedValue[T](AIModel):
    """Optional typed patch with evidence from only the current user message."""

    value: T | None = None
    provided: bool = False
    ambiguous: bool = False
    correction: bool = False
    evidence: str = Field(default="", max_length=500)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    alternatives: list[str] = Field(default_factory=list, max_length=5)


class ExtractedLocation(AIModel):
    raw_value: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    zone: str | None = Field(default=None, max_length=120)
    provided: bool = False
    ambiguous: bool = False
    correction: bool = False
    explicit_confirmation: bool = False
    evidence: str = Field(default="", max_length=500)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("raw_value", "city", "zone")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()).strip() if value and value.strip() else None


class ExtractedDeliveryExperience(AIModel):
    years: float | None = Field(default=None, ge=0.0, le=60.0)
    platforms: list[str] = Field(default_factory=list, max_length=12)
    provided: bool = False
    ambiguous: bool = False
    correction: bool = False
    evidence: str = Field(default="", max_length=500)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class StartAvailabilityExtraction(AIModel):
    raw_value: str | None = Field(default=None, max_length=300)
    date: date_type | None = None
    precision: str = Field(default="unknown", max_length=20)
    timezone: str | None = Field(default=None, max_length=80)
    provided: bool = False
    ambiguous: bool = False
    correction: bool = False
    evidence: str = Field(default="", max_length=500)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TurnInterpretation(AIModel):
    """One model interpretation of one candidate message."""

    detected_language: Language = Language.ES
    language_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    explicit_language: Language | None = None
    intent: TurnIntent = TurnIntent.ANSWER
    response_requested: bool = False
    prompt_injection_detected: bool = False
    sensitive_data_detected: bool = False
    opt_out_requested: bool = False
    # This is an acknowledgement of the assistant's disclosure, not consent
    # or an eligibility signal.  It is kept separate from final_confirmation.
    disclosure_acknowledged: bool | None = None
    confirmation: bool | None = None
    final_confirmation: bool | None = None
    full_name: ExtractedValue[str] | None = None
    drivers_license: ExtractedValue[bool] | None = None
    location: ExtractedLocation | None = None
    availability: ExtractedValue[list[AvailabilityType]] | None = None
    preferred_schedule: ExtractedValue[SchedulePreference] | None = None
    delivery_experience: ExtractedDeliveryExperience | None = None
    start_availability: StartAvailabilityExtraction | None = None
    candidate_questions: list[str] = Field(default_factory=list, max_length=3)
    ambiguity_notes: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("candidate_questions", "ambiguity_notes")
    @classmethod
    def clean_short_text(cls, values: list[str]) -> list[str]:
        return [" ".join(value.split())[:500] for value in values if value.strip()]


class RecruiterSummaryOutput(AIModel):
    """Bounded factual summary generated from validated state only."""

    summary: str = Field(min_length=1, max_length=1_200)

    @field_validator("summary")
    @classmethod
    def clean_summary(cls, value: str) -> str:
        return " ".join(value.split()).strip()
