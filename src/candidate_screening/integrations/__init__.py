"""Boundary types for optional external integrations."""

from .ats import (
    ATSAdapter,
    ATSCandidate,
    ATSCandidateDTO,
    ATSClient,
    ATSResult,
    ATSResultDTO,
    ATSScreening,
    ATSScreeningDTO,
    ATSSubmissionDTO,
)

__all__ = [
    "ATSClient",
    "ATSAdapter",
    "ATSCandidate",
    "ATSCandidateDTO",
    "ATSResult",
    "ATSResultDTO",
    "ATSScreening",
    "ATSScreeningDTO",
    "ATSSubmissionDTO",
]
