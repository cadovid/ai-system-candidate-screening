"""Small dependency and serialization helpers shared by route modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request

from candidate_screening.application.coordinator import (
    ConversationCreated,
    ConversationView,
    CoordinatorError,
    ScreeningView,
    TurnCoordinator,
    TurnCoordinatorResult,
)

from ..schemas import (
    CandidateConversationResponse,
    CandidateTurnResponse,
    CreateConversationResponse,
    DecisionDTO,
    MessageDTO,
    RecruiterScreeningResponse,
)


def get_coordinator(request: Request) -> TurnCoordinator:
    """Resolve the coordinator lazily, allowing deterministic API tests.

    ``create_app`` stores a factory instead of opening a database connection at
    import time.  Injected coordinators are returned unchanged.
    """

    coordinator = getattr(request.app.state, "coordinator", None)
    if coordinator is not None:
        return coordinator
    factory = getattr(request.app.state, "coordinator_factory", None)
    if factory is None:
        raise CoordinatorError(
            "service_unavailable",
            "The screening service is temporarily unavailable.",
            status_code=503,
        )
    try:
        coordinator = factory()
    except CoordinatorError:
        raise
    except Exception as exc:
        raise CoordinatorError(
            "service_unavailable",
            "The screening service is temporarily unavailable.",
            status_code=503,
        ) from exc
    request.app.state.coordinator = coordinator
    return coordinator


def correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unknown"))


def decision_dto(decision: Any) -> DecisionDTO | None:
    if decision is None:
        return None
    payload = (
        decision.model_dump(mode="json") if hasattr(decision, "model_dump") else dict(decision)
    )
    return DecisionDTO.model_validate(payload)


def message_dto(message: Mapping[str, Any]) -> MessageDTO:
    return MessageDTO.model_validate(message)


def created_response(created: ConversationCreated) -> CreateConversationResponse:
    return CreateConversationResponse(
        conversation_id=created.conversation_id,
        session_id=created.session_id,
        resume_token=created.resume_token,
        language=created.language,
        assistant_message=created.assistant_message,
        state_version=created.state_version,
    )


def turn_response(result: TurnCoordinatorResult) -> CandidateTurnResponse:
    from candidate_screening.domain.enums import TurnStatus

    return CandidateTurnResponse(
        conversation_id=result.conversation_id,
        turn_id=result.turn_id,
        status=result.status,
        screening_status=result.screening_status,
        assistant_message=result.assistant_message,
        state_version=result.state_version,
        next_field=result.next_field,
        decision=decision_dto(result.decision),
        idempotent=result.idempotent,
        retryable=result.status is TurnStatus.FAILED,
        error_code=result.error_code,
        summary_status=result.summary_status,
    )


def candidate_view(view: ConversationView) -> CandidateConversationResponse:
    return CandidateConversationResponse(
        conversation_id=view.conversation_id,
        session_id=view.session_id,
        status=view.status.value,
        language=view.language,
        state_version=view.state_version,
        decision=decision_dto(view.decision),
        messages=[message_dto(message) for message in view.messages],
    )


def recruiter_view(view: ScreeningView) -> RecruiterScreeningResponse:
    return RecruiterScreeningResponse(
        session_id=view.session_id,
        conversation_id=view.conversation_id,
        candidate_id=view.candidate_id,
        candidate_name=view.candidate_name,
        status=view.status,
        state=view.state.model_dump(mode="json"),
        state_version=view.state_version,
        decision=decision_dto(view.decision),
        summary=view.summary,
        summary_status=view.summary_status,
        handoff_status=view.handoff_status,
        messages=[message_dto(message) for message in view.messages],
    )


__all__ = [
    "candidate_view",
    "correlation_id",
    "created_response",
    "decision_dto",
    "get_coordinator",
    "message_dto",
    "recruiter_view",
    "turn_response",
]
