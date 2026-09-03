"""Candidate conversation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse

from candidate_screening.api.auth import require_candidate_token
from candidate_screening.api.schemas import (
    CandidateConversationResponse,
    CandidateTurnRequest,
    CandidateTurnResponse,
    CreateConversationRequest,
    CreateConversationResponse,
)
from candidate_screening.application.coordinator import CoordinatorError
from candidate_screening.domain.enums import TurnStatus

from .common import candidate_view, correlation_id, created_response, get_coordinator, turn_response

router = APIRouter(tags=["candidate"])


@router.post(
    "/conversations", response_model=CreateConversationResponse, status_code=status.HTTP_201_CREATED
)
async def create_candidate_conversation(
    payload: CreateConversationRequest,
    request: Request,
) -> CreateConversationResponse:
    created = await get_coordinator(request).create_conversation(
        language=payload.language,
        channel=payload.channel,
        candidate_name=payload.candidate_name,
    )
    return created_response(created)


@router.get("/conversations/{conversation_id}", response_model=CandidateConversationResponse)
async def get_candidate_conversation(
    conversation_id: str,
    request: Request,
) -> CandidateConversationResponse:
    coordinator = get_coordinator(request)
    await require_candidate_token(request, conversation_id, coordinator)
    return candidate_view(await coordinator.get_conversation(conversation_id))


@router.post("/conversations/{conversation_id}/turns", response_model=CandidateTurnResponse)
async def candidate_turn(
    conversation_id: str,
    payload: CandidateTurnRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> JSONResponse | CandidateTurnResponse:
    coordinator = get_coordinator(request)
    await require_candidate_token(request, conversation_id, coordinator)
    header_key = idempotency_key or x_idempotency_key
    if payload.idempotency_key and header_key and payload.idempotency_key != header_key:
        raise CoordinatorError(
            "idempotency_key_mismatch", "idempotency keys do not match", status_code=422
        )
    key = payload.idempotency_key or header_key
    if not key:
        raise CoordinatorError(
            "missing_idempotency_key", "Idempotency-Key is required", status_code=422
        )
    result = await coordinator.process_turn(
        conversation_id,
        payload.message,
        key,
        correlation_id=correlation_id(request),
        language=payload.language,
        input_mode=payload.input_mode,
    )
    response = turn_response(result)
    if result.status is TurnStatus.FAILED:
        return JSONResponse(
            response.model_dump(mode="json"),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"X-Correlation-ID": correlation_id(request)},
        )
    return response


__all__ = ["router"]
