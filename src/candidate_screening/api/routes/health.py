"""Unauthenticated liveness/readiness endpoints.

``/livez`` intentionally has no dependency checks so an unhealthy dependency
does not cause an orchestrator restart loop.  ``/readyz`` is deliberately
narrow: it verifies the configured fixture files can be parsed and that the
database accepts a trivial query.  Provider availability is not a readiness
condition because the application has a safe no-key mode and provider calls
are request-scoped.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from candidate_screening import __version__
from candidate_screening.application.faq import FAQCatalog
from candidate_screening.config import Settings
from candidate_screening.domain.service_areas import ServiceAreaMatcher
from candidate_screening.persistence.database import create_async_engine_for_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["operations"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "candidate-screening", "version": __version__}


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "alive"}


async def _database_check(settings: Settings, existing_engine: AsyncEngine | None) -> None:
    """Run one cheap database query and close a temporary engine if needed."""

    engine = existing_engine or create_async_engine_for_url(settings.database_url)
    temporary = existing_engine is None
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        if temporary:
            await engine.dispose()


def _fixture_check(settings: Settings) -> None:
    """Parse both versioned JSON fixtures used by the default coordinator."""

    ServiceAreaMatcher.from_file(settings.service_areas_path)
    FAQCatalog.from_file(settings.faq_path)


@router.get("/readyz", response_model=None)
async def readyz(request: Request) -> dict[str, Any] | JSONResponse:
    """Report readiness only when database and bundled fixtures are usable."""

    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        return JSONResponse(
            {"status": "not_ready", "checks": {"database": "unknown", "fixtures": "unknown"}},
            status_code=503,
        )

    fixture_status = "ok"
    database_status = "ok"
    try:
        _fixture_check(settings)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        fixture_status = "failed"
        logger.warning(
            "readiness fixture check failed",
            extra={"check": "fixtures", "error_type": type(exc).__name__},
        )

    try:
        existing_engine = getattr(request.app.state, "engine", None)
        await _database_check(
            settings, existing_engine if isinstance(existing_engine, AsyncEngine) else None
        )
    except Exception as exc:  # dependency errors must become a safe 503 response
        database_status = "failed"
        logger.warning(
            "readiness database check failed",
            extra={"check": "database", "error_type": type(exc).__name__},
        )

    checks = {"database": database_status, "fixtures": fixture_status}
    if database_status != "ok" or fixture_status != "ok":
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return {"status": "ready", "checks": checks}


__all__ = ["router"]
