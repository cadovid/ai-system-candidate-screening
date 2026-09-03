"""FastAPI entrypoint for candidate and recruiter clients."""

# FastAPI registers the handlers below through decorators, which Pyright
# cannot observe as ordinary call sites.
# pyright: reportUnusedFunction=false

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .ai.interpreter import InterpreterResult
from .ai.pydantic_ai import PydanticAIInterpreter, SummaryGenerator
from .api.limits import SlidingWindowRateLimiter, TurnConcurrencyLimit
from .api.middleware import CorrelationMiddleware, RequestLimitsMiddleware
from .api.routes import analytics_router, candidate_router, health_router, internal_router
from .api.schemas import ErrorBody, ErrorResponse
from .application.conversation import ConversationController
from .application.coordinator import CoordinatorError, TurnCoordinator
from .application.faq import FAQCatalog
from .config import Settings, get_settings
from .domain.rules import ScreeningEngine
from .domain.service_areas import ServiceAreaMatcher
from .observability import configure_logging
from .persistence.database import create_async_engine_for_url, create_session_factory
from .persistence.unit_of_work import SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = Jinja2Templates(directory=str(_ROOT / "templates"))


class _UnavailableInterpreter:
    """Lazy default used so health checks and conversation creation need no key."""

    async def interpret(
        self, message: str, dependencies: Any, *, message_history: Any = ()
    ) -> InterpreterResult:
        raise RuntimeError("provider is not configured")


def _build_default_coordinator(app: FastAPI, settings: Settings) -> TurnCoordinator:
    engine = create_async_engine_for_url(settings.database_url)
    factory = create_session_factory(engine)
    matcher = ServiceAreaMatcher.from_file(settings.service_areas_path)
    faq = FAQCatalog.from_file(settings.faq_path)
    controller = ConversationController(
        ScreeningEngine(ruleset_version="2026-01"),
        matcher,
        faq_catalog=faq,
    )
    if settings.has_selected_provider_credentials():
        interpreter: Any = PydanticAIInterpreter(settings)
        summary: Any = SummaryGenerator(settings)
    else:
        logger.warning(
            "selected LLM provider is not configured",
            extra={
                "llm_provider": settings.llm_provider,
                "required_environment_variable": settings.selected_provider_key_variable,
            },
        )
        interpreter = _UnavailableInterpreter()
        summary = None
    coordinator = TurnCoordinator(
        lambda: SqlAlchemyUnitOfWork(factory),
        controller,
        interpreter,
        summary_generator=summary,
        history_max_pairs=settings.history_max_pairs,
        history_max_characters=settings.history_max_characters,
        max_input_characters=settings.max_input_characters,
        max_turns=settings.max_turns,
    )
    app.state.engine = engine
    return coordinator


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unknown"))


def _error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    correlation_id = _correlation_id(request)
    payload = ErrorResponse(
        error=ErrorBody(code=code, message=message, correlation_id=correlation_id)
    ).model_dump(mode="json")
    return JSONResponse(
        payload, status_code=status_code, headers={"X-Correlation-ID": correlation_id}
    )


def create_app(
    *,
    settings: Settings | None = None,
    coordinator: TurnCoordinator | None = None,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    turn_concurrency: TurnConcurrencyLimit | None = None,
) -> FastAPI:
    """Create an injectable application for tests, workers, and local use."""

    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    application = FastAPI(
        title="AI Candidate Screening Assistant",
        version=__version__,
        description="A controlled bilingual delivery-driver screening service.",
    )
    application.state.settings = runtime_settings
    application.state.coordinator = coordinator
    application.state.coordinator_factory = lambda: _build_default_coordinator(
        application, runtime_settings
    )
    application.state.rate_limiter = rate_limiter or SlidingWindowRateLimiter(
        runtime_settings.rate_limit_requests,
        runtime_settings.rate_limit_window_seconds,
    )
    application.state.turn_concurrency = turn_concurrency or TurnConcurrencyLimit(
        runtime_settings.max_concurrent_turns
    )
    application.add_middleware(CorrelationMiddleware)
    application.add_middleware(
        RequestLimitsMiddleware,
        max_body_bytes=runtime_settings.max_body_bytes,
        rate_limiter=application.state.rate_limiter,
        turn_concurrency=application.state.turn_concurrency,
    )
    static_dir = _ROOT / "static"
    if static_dir.exists():
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @application.exception_handler(CoordinatorError)
    async def coordinator_error_handler(request: Request, exc: CoordinatorError) -> JSONResponse:
        return _error_response(request, exc.code, exc.message, exc.status_code)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        _ = exc
        return _error_response(request, "invalid_request", "The request is invalid.", 422)

    @application.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail: dict[str, object] = (
            cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
        )
        return _error_response(
            request,
            str(detail.get("code", "http_error")),
            str(detail.get("message", "The request could not be completed.")),
            exc.status_code,
        )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def candidate_page(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "candidate.html", {"version": __version__})

    @application.get("/recruiter", response_class=HTMLResponse, include_in_schema=False)
    async def recruiter_page(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "recruiter.html", {"version": __version__})

    # Keep the versioned API and a short unversioned alias for local demos.
    # The alias is deliberately omitted from OpenAPI so clients have one
    # canonical contract to target.
    application.include_router(candidate_router, prefix="/api/v1/candidate")
    application.include_router(candidate_router, prefix="/candidate", include_in_schema=False)
    application.include_router(internal_router, prefix="/api/v1/internal")
    application.include_router(internal_router, prefix="/internal", include_in_schema=False)
    application.include_router(analytics_router, prefix="/api/v1/internal")
    application.include_router(analytics_router, prefix="/internal", include_in_schema=False)
    application.include_router(analytics_router, prefix="/api/v1", include_in_schema=False)
    application.include_router(analytics_router, prefix="", include_in_schema=False)
    application.include_router(health_router)

    return application


app = create_app()

__all__ = ["app", "create_app"]
