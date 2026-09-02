"""Protocol and data-transfer objects for an ATS adapter.

This module intentionally contains no HTTP client, credentials, retries, or
provider-specific behaviour.  An application can implement ``ATSClient`` in a
separate adapter and test the screening system against a fake implementation.
Keeping the boundary here prevents a future ATS integration from becoming an
implicit source of eligibility decisions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from candidate_screening.domain.enums import Language, ScreeningStatus


class ATSModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ATSCandidateDTO(ATSModel):
    """Minimal candidate identity used at an ATS hand-off boundary."""

    candidate_id: str = Field(min_length=1, max_length=100)
    full_name: str | None = Field(default=None, max_length=200)
    external_reference: str | None = Field(default=None, max_length=200)

    @field_validator("candidate_id", "full_name", "external_reference")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()).strip() if value is not None else None


class ATSScreeningDTO(ATSModel):
    """Factual screening hand-off; status is produced by the domain engine."""

    screening_id: str = Field(min_length=1, max_length=100)
    candidate_id: str = Field(min_length=1, max_length=100)
    status: ScreeningStatus
    state: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    ruleset_version: str = Field(default="2026-01", min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=1_200)
    summary_status: str | None = Field(default=None, max_length=32)
    handoff_status: str | None = Field(default=None, max_length=32)
    preferred_language: Language = Language.ES
    completed_at: datetime | None = None

    @field_validator(
        "screening_id",
        "candidate_id",
        "ruleset_version",
        "summary",
        "summary_status",
        "handoff_status",
    )
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()).strip() if value is not None else None

    @field_validator("reason_codes")
    @classmethod
    def clean_reason_codes(cls, values: list[str]) -> list[str]:
        return [" ".join(value.split()).strip() for value in values if value.strip()]


class ATSSubmissionDTO(ATSModel):
    """Provider-neutral acknowledgement returned by an ATS adapter."""

    accepted: bool
    external_id: str | None = Field(default=None, max_length=200)
    status: str = Field(default="accepted", min_length=1, max_length=32)
    submitted_at: datetime | None = None
    message: str | None = Field(default=None, max_length=500)


@runtime_checkable
class ATSClient(Protocol):
    """Optional adapter contract implemented outside the screening domain."""

    async def submit_screening(self, screening: ATSScreeningDTO) -> ATSSubmissionDTO:
        """Submit a factual screening result and return an acknowledgement."""

        ...


# Compatibility names make the boundary easy to consume without coupling
# application code to an acronym-heavy class name.
CandidateDTO = ATSCandidateDTO
ScreeningDTO = ATSScreeningDTO
ATSResultDTO = ATSSubmissionDTO
ATSSubmissionResult = ATSSubmissionDTO
ATSIntegrationProtocol = ATSClient
ATSAdapter = ATSClient
ATSCandidate = ATSCandidateDTO
ATSScreening = ATSScreeningDTO
ATSResult = ATSSubmissionDTO

__all__ = [
    "ATSClient",
    "ATSAdapter",
    "ATSIntegrationProtocol",
    "ATSCandidateDTO",
    "ATSCandidate",
    "ATSModel",
    "ATSResultDTO",
    "ATSResult",
    "ATSScreeningDTO",
    "ATSScreening",
    "ATSSubmissionDTO",
    "ATSSubmissionResult",
    "CandidateDTO",
    "ScreeningDTO",
]
