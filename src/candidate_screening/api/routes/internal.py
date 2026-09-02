"""Recruiter/internal screening endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from candidate_screening.api.auth import require_internal_key
from candidate_screening.api.schemas import (
    RecruiterListResponse,
    RecruiterScreeningResponse,
    ReviewRequest,
    ReviewResponse,
)
from candidate_screening.domain.enums import ScreeningStatus

from .common import get_coordinator, recruiter_view

router = APIRouter(tags=["internal"])


async def _authenticate(request: Request) -> None:
    settings = request.app.state.settings
    require_internal_key(request, settings.internal_api_key)


@router.get("/screenings", response_model=RecruiterListResponse)
async def list_internal_screenings(
    request: Request,
    screening_status: Annotated[ScreeningStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecruiterListResponse:
    await _authenticate(request)
    views = await get_coordinator(request).list_screenings(
        status=screening_status,
        limit=limit,
        offset=offset,
    )
    return RecruiterListResponse(
        items=[recruiter_view(view) for view in views], limit=limit, offset=offset
    )


@router.get("/screenings/{session_id}", response_model=RecruiterScreeningResponse)
async def get_internal_screening(session_id: str, request: Request) -> RecruiterScreeningResponse:
    await _authenticate(request)
    return recruiter_view(await get_coordinator(request).get_screening(session_id))


@router.post("/screenings/{session_id}/summary/retry", response_model=RecruiterScreeningResponse)
async def retry_internal_summary(session_id: str, request: Request) -> RecruiterScreeningResponse:
    """Protected repair endpoint for summaries left pending by a transient failure."""

    await _authenticate(request)
    return recruiter_view(await get_coordinator(request).retry_summary(session_id))


@router.post(
    "/screenings/{session_id}/reviews",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def review_internal_screening(
    session_id: str,
    payload: ReviewRequest,
    request: Request,
) -> ReviewResponse:
    await _authenticate(request)
    review = await get_coordinator(request).record_review(
        session_id,
        payload.reviewer_id,
        payload.decision,
        payload.notes,
    )
    return ReviewResponse.model_validate(review)


__all__ = ["router"]
