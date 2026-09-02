"""Read-only aggregate analytics for authorized internal clients."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, cast

from fastapi import APIRouter, Request

from candidate_screening.api.auth import require_internal_key
from candidate_screening.api.schemas import AnalyticsResponse
from candidate_screening.application.coordinator import CoordinatorError

from .common import get_coordinator

router = APIRouter(tags=["analytics"])


@router.get("/analytics", response_model=AnalyticsResponse)
async def screening_analytics(request: Request) -> AnalyticsResponse:
    settings = request.app.state.settings
    require_internal_key(request, settings.internal_api_key)
    coordinator = get_coordinator(request)
    get_analytics = getattr(coordinator, "get_analytics", None)
    if get_analytics is None:
        raise CoordinatorError(
            "analytics_unavailable",
            "Analytics are temporarily unavailable.",
            status_code=503,
        )
    analytics = await get_analytics()
    payload = asdict(cast(Any, analytics)) if is_dataclass(analytics) else analytics
    return AnalyticsResponse.model_validate(payload)


__all__ = ["router"]
