"""Versioned HTTP DTOs for candidate and recruiter clients."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from candidate_screening.domain.enums import Language, ScreeningStatus, TurnStatus


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateConversationRequest(APIModel):
    language: Language = Language.ES
    channel: str = Field(default="web", min_length=1, max_length=32)
    candidate_name: str | None = Field(default=None, max_length=200)

    @field_validator("channel", "candidate_name")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()).strip() if value is not None else None


class CreateConversationResponse(APIModel):
    conversation_id: str
    session_id: str
    resume_token: str
    language: Language
    assistant_message: str
    state_version: int = 1


class CandidateTurnRequest(APIModel):
    message: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    # The browser language selector sends this as an explicit, auditable
    # preference change.  It is optional for existing API clients.
    language: Language | None = None

    @field_validator("message", "idempotency_key")
    @classmethod
    def trim_request_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()).strip() if value is not None else None


class DecisionDTO(APIModel):
    status: ScreeningStatus
    reason_codes: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=lambda: list[dict[str, Any]]())
    ruleset_version: str = "2026-01"
    rule_trace: dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime | None = None


class CandidateTurnResponse(APIModel):
    conversation_id: str
    turn_id: str
    status: TurnStatus
    screening_status: ScreeningStatus
    assistant_message: str
    state_version: int
    next_field: str | None = None
    decision: DecisionDTO | None = None
    idempotent: bool = False
    retryable: bool = False
    error_code: str | None = None
    summary_status: str | None = None


class MessageDTO(APIModel):
    id: str
    direction: str
    content: str
    language: Language | None = None
    created_at: datetime


class CandidateConversationResponse(APIModel):
    conversation_id: str
    session_id: str
    status: str
    language: Language
    state_version: int
    decision: DecisionDTO | None = None
    messages: list[MessageDTO] = Field(default_factory=lambda: list[MessageDTO]())


class RecruiterScreeningResponse(APIModel):
    session_id: str
    conversation_id: str
    candidate_id: str
    candidate_name: str | None = None
    status: ScreeningStatus
    state: dict[str, Any]
    state_version: int
    decision: DecisionDTO | None = None
    summary: str | None = None
    summary_status: str | None = None
    handoff_status: str | None = None
    messages: list[MessageDTO] = Field(default_factory=lambda: list[MessageDTO]())


class RecruiterListResponse(APIModel):
    items: list[RecruiterScreeningResponse]
    limit: int
    offset: int


class AnalyticsResponse(APIModel):
    """Small aggregate view for recruiter operations dashboards."""

    total_screenings: int = Field(ge=0)
    status_counts: dict[str, int] = Field(default_factory=dict)
    total_turns: int = Field(ge=0)
    completed_turns: int = Field(ge=0)
    failed_turns: int = Field(ge=0)
    average_turn_latency_ms: float | None = Field(default=None, ge=0)
    summaries_generated: int = Field(default=0, ge=0)
    summaries_fallback: int = Field(default=0, ge=0)
    handoffs_ready: int = Field(default=0, ge=0)
    reviews_recorded: int = Field(default=0, ge=0)
    total_events: int = Field(default=0, ge=0)
    event_counts: dict[str, int] = Field(default_factory=dict)
    guardrail_blocks: int = Field(default=0, ge=0)
    reengagements_sent: int = Field(default=0, ge=0)
    reengagements_suppressed: int = Field(default=0, ge=0)
    screenings_started: int = Field(default=0, ge=0)
    screenings_completed: int = Field(default=0, ge=0)
    screenings_in_progress: int = Field(default=0, ge=0)
    screenings_abandoned: int = Field(default=0, ge=0)
    turns_started: int = Field(default=0, ge=0)
    qualified: int = Field(default=0, ge=0)
    disqualified: int = Field(default=0, ge=0)
    needs_review: int = Field(default=0, ge=0)
    opted_out: int = Field(default=0, ge=0)
    summaries_pending: int = Field(default=0, ge=0)
    completion_rate: float | None = Field(default=None, ge=0, le=1)
    average_completed_messages: float | None = Field(default=None, ge=0)
    average_completed_turns: float | None = Field(default=None, ge=0)
    average_screening_duration_seconds: float | None = Field(default=None, ge=0)
    language_distribution: dict[str, int] = Field(default_factory=dict)
    faq_usage_count: int = Field(default=0, ge=0)
    clarification_retry_count: int = Field(default=0, ge=0)
    clarification_retry_counts: dict[str, int] = Field(default_factory=dict)
    disqualification_reason_distribution: dict[str, int] = Field(default_factory=dict)
    dropoff_stage_distribution: dict[str, int] = Field(default_factory=dict)


class ReviewRequest(APIModel):
    reviewer_id: str = Field(min_length=1, max_length=200)
    decision: str = Field(min_length=1, max_length=32)
    notes: str | None = Field(default=None, max_length=2_000)

    @field_validator("reviewer_id", "decision", "notes")
    @classmethod
    def trim_review_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()).strip() if value is not None else None


class ReviewResponse(APIModel):
    id: str
    screening_session_id: str
    reviewer_id: str
    decision: str
    notes: str | None = None
    created_at: datetime


class ErrorBody(APIModel):
    code: str
    message: str
    correlation_id: str


class ErrorResponse(APIModel):
    error: ErrorBody


__all__ = [
    "AnalyticsResponse",
    "CandidateConversationResponse",
    "CandidateTurnRequest",
    "CandidateTurnResponse",
    "CreateConversationRequest",
    "CreateConversationResponse",
    "DecisionDTO",
    "ErrorBody",
    "ErrorResponse",
    "MessageDTO",
    "RecruiterListResponse",
    "RecruiterScreeningResponse",
    "ReviewRequest",
    "ReviewResponse",
]
