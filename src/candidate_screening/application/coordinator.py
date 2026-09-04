"""Transactional boundary for one candidate conversation.

``TurnCoordinator`` is deliberately boring about business decisions: it
reserves a turn, calls the interpreter outside any database transaction,
applies the deterministic controller, and then commits the result using an
optimistic state version.  This keeps provider latency from holding database
connections while still making retries and concurrent requests safe.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from candidate_screening.ai.history import HistoryMessage, build_bounded_history
from candidate_screening.ai.interpreter import (
    AIProviderError,
    InterpreterDependencies,
    InterpreterResult,
    LanguageInterpreter,
    SummaryGeneratorProtocol,
)
from candidate_screening.ai.schemas import (
    ExtractedValue,
    TurnIntent,
    TurnInterpretation,
)
from candidate_screening.application.analytics import (
    AnalyticsEvent,
    AnalyticsEventType,
    aggregate_events,
    aggregate_persisted_data,
)
from candidate_screening.application.conversation import (
    ConversationController,
    ConversationTurnResult,
)
from candidate_screening.application.deterministic_interpretation import (
    interpret_deterministically,
    recover_location_answer,
)
from candidate_screening.application.guardrails import inspect_message, summary_is_safe
from candidate_screening.domain.enums import (
    ConversationStatus,
    InteractionMode,
    Language,
    ScreeningField,
    ScreeningStatus,
    TurnStatus,
)
from candidate_screening.domain.models import ScreeningDecision, ScreeningState
from candidate_screening.domain.transitions import assert_transition
from candidate_screening.persistence.orm import (
    AuditEventORM,
    CandidateORM,
    ConversationORM,
    MessageORM,
    RecruiterReviewORM,
    ScreeningResultORM,
    ScreeningSessionORM,
    TurnORM,
)
from candidate_screening.persistence.unit_of_work import SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)


def generate_resume_token() -> str:
    """Generate a high-entropy opaque token suitable for a bearer credential."""

    return secrets.token_urlsafe(32)


def hash_resume_token(token: str) -> str:
    """Hash a resume token before it reaches storage or a query."""

    value = token.strip()
    if not value:
        raise ValueError("resume token cannot be blank")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_hash(message: str) -> str:
    return hashlib.sha256(message.strip().encode("utf-8")).hexdigest()


# These are deliberately small, closed vocabularies.  They are used only for
# unambiguous control answers and never attempt to replace the language model
# for natural-language extraction.
_CONFIRMATION_VALUES: dict[str, bool] = {
    "yes": True,
    "y": True,
    "sí": True,
    "si": True,
    "true": True,
    "correct": True,
    "correcto": True,
    "vale": True,
    "no": False,
    "n": False,
    "false": False,
    "nope": False,
}
_LICENSE_VALUES: dict[str, bool] = {
    "yes": True,
    "y": True,
    "sí": True,
    "si": True,
    "no": False,
    "n": False,
    "nope": False,
}
_DISCLOSURE_ACKNOWLEDGEMENTS = frozenset(
    {
        "yes",
        "y",
        "sí",
        "si",
        "yes please",
        "sí por favor",
        "si por favor",
        "sure",
        "okay",
        "ok",
        "vale",
        "de acuerdo",
        "adelante",
        "go on",
        "go ahead",
        "continue",
        "let's continue",
        "lets continue",
        "start",
        "start please",
        "empecemos",
        "empecemos por favor",
        "empezamos",
        "comencemos",
        "continuar",
        "continuemos",
    }
)
_NAME_PREFIX = re.compile(
    r"^(?:my\s+name\s+is|name\s+is|i\s+am|i['’]?m|me\s+llamo|mi\s+nombre\s+es)\s+(.+?)\s*[.!?]?$",
    re.IGNORECASE,
)
_NAME_TOKEN = re.compile(r"^[^\W\d_]+(?:[-'’][^\W\d_]+)*$", re.UNICODE)
_NON_NAME_TOKENS = frozenset(
    {
        "adelante",
        "and",
        "area",
        "at",
        "available",
        "años",
        "año",
        "begin",
        "can",
        "center",
        "city",
        "ciudad",
        "completo",
        "comencemos",
        "continue",
        "continuar",
        "continuemos",
        "delivery",
        "empecemos",
        "empezamos",
        "es",
        "experience",
        "favor",
        "fines",
        "from",
        "full",
        "gracias",
        "go",
        "have",
        "has",
        "home",
        "interesado",
        "interested",
        "is",
        "live",
        "lives",
        "llamo",
        "licencia",
        "license",
        "mi",
        "my",
        "name",
        "no",
        "on",
        "ok",
        "okay",
        "part",
        "parcial",
        "please",
        "por",
        "reparto",
        "schedule",
        "si",
        "sí",
        "start",
        "sure",
        "tengo",
        "thanks",
        "tiene",
        "tiempo",
        "trabajar",
        "trabajo",
        "valid",
        "vigente",
        "weekends",
        "years",
        "y",
        "zona",
        "yes",
        "central",
        "centro",
        "east",
        "este",
        "north",
        "norte",
        "south",
        "sur",
        "west",
        "oeste",
        "zone",
    }
)


def _normalize_short_message(message: str) -> str:
    """Normalize exact control replies without interpreting free-form prose."""

    normalized = re.sub(r"[.,!?;:¡¿]+", " ", message.casefold())
    return " ".join(normalized.split())


def _extract_simple_full_name(message: str) -> str | None:
    """Return a conservative name-only answer, or ``None`` for free-form text.

    The parser is intentionally limited to a short alphabetic answer (or an
    explicit name prefix). Bare names must look like a proper-name phrase.
    Multi-field answers, questions, and prose stay on the model path so this
    optimization cannot make a business decision from an ambiguous message.
    """

    collapsed = " ".join(message.split()).strip()
    prefix_match = _NAME_PREFIX.match(collapsed)
    candidate = prefix_match.group(1) if prefix_match else collapsed
    candidate = candidate.strip(" .,!?;:¡¿")
    if not candidate or any(marker in candidate for marker in (",", ";", "|")):
        return None
    words = candidate.split()
    # A one-word response is too easy to confuse with an acknowledgement or an
    # unrelated answer. Keep those on the model path even with an explicit
    # prefix; the existing schema does not require us to guess a full name.
    if len(words) < 2 or len(words) > 6:
        return None
    # An unprefixed answer is treated as a name only when it looks like a
    # proper-name phrase. This keeps common short prose (for example
    # ``spoken answer``) and location phrases (for example ``Madrid centro``)
    # on the model path. Explicit prefixes such as ``Me llamo ...`` may use
    # normal sentence casing.
    if prefix_match is None and any(not word[0].isupper() for word in words):
        return None
    if any(not _NAME_TOKEN.fullmatch(word) for word in words):
        return None
    if any(word.casefold() in _NON_NAME_TOKENS for word in words):
        return None
    return candidate[:200]


class CoordinatorError(RuntimeError):
    """Safe, client-facing coordinator failure with no provider details."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class _UOWFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[SqlAlchemyUnitOfWork]: ...


@dataclass(frozen=True, slots=True)
class ConversationCreated:
    conversation_id: str
    session_id: str
    resume_token: str
    language: Language
    assistant_message: str
    state_version: int = 1


@dataclass(frozen=True, slots=True)
class TurnCoordinatorResult:
    conversation_id: str
    turn_id: str
    status: TurnStatus
    screening_status: ScreeningStatus
    assistant_message: str
    state_version: int
    next_field: str | None = None
    decision: ScreeningDecision | None = None
    idempotent: bool = False
    error_code: str | None = None
    summary_status: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationView:
    conversation_id: str
    session_id: str
    status: ConversationStatus
    language: Language
    state: ScreeningState
    state_version: int
    decision: ScreeningDecision | None
    messages: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ScreeningView:
    session_id: str
    conversation_id: str
    candidate_id: str
    candidate_name: str | None
    status: ScreeningStatus
    state: ScreeningState
    state_version: int
    decision: ScreeningDecision | None
    summary: str | None
    summary_status: str | None
    handoff_status: str | None
    messages: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class AnalyticsView:
    """Aggregate operational counters; no candidate-level data is exposed."""

    total_screenings: int
    status_counts: dict[str, int]
    total_turns: int
    completed_turns: int
    failed_turns: int
    average_turn_latency_ms: float | None
    summaries_generated: int
    summaries_fallback: int
    handoffs_ready: int
    reviews_recorded: int
    # Event-level counters are additive so existing clients that only consume
    # the original operational counters remain compatible.
    total_events: int = 0
    event_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    guardrail_blocks: int = 0
    reengagements_sent: int = 0
    reengagements_suppressed: int = 0
    # Additional planned operational metrics.  Defaults keep this DTO
    # backwards-compatible with integrations that construct it positionally.
    screenings_started: int = 0
    screenings_completed: int = 0
    screenings_in_progress: int = 0
    screenings_abandoned: int = 0
    turns_started: int = 0
    qualified: int = 0
    disqualified: int = 0
    needs_review: int = 0
    opted_out: int = 0
    summaries_pending: int = 0
    completion_rate: float | None = None
    # Persisted-data aggregates.  They intentionally contain no candidate
    # identifiers or message content and default to an empty/unknown value so
    # older adapters can still construct this DTO positionally.
    average_completed_messages: float | None = None
    average_completed_turns: float | None = None
    average_screening_duration_seconds: float | None = None
    language_distribution: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    faq_usage_count: int = 0
    clarification_retry_count: int = 0
    clarification_retry_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    disqualification_reason_distribution: dict[str, int] = field(
        default_factory=lambda: dict[str, int]()
    )
    dropoff_stage_distribution: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    interaction_mode_distribution: dict[str, int] = field(default_factory=lambda: dict[str, int]())


@dataclass(frozen=True, slots=True)
class _Reservation:
    conversation_id: str
    session_id: str
    turn_id: str
    request_hash: str
    state: ScreeningState
    version: int
    language: Language
    history: tuple[HistoryMessage, ...]
    stored_message: str
    turn_number: int
    turn_limit_reached: bool = False
    started_at: float = field(default_factory=time.monotonic)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_status(value: str | ScreeningStatus) -> ScreeningStatus:
    try:
        return value if isinstance(value, ScreeningStatus) else ScreeningStatus(value)
    except ValueError:
        return ScreeningStatus.IN_PROGRESS


def _summary_fallback(
    state: ScreeningState, decision: ScreeningDecision, language: Language
) -> str:
    """Build a factual summary from canonical fields only.

    It intentionally omits free-form evidence and raw start-date text.  The
    resulting text is safe even when a provider is unavailable or returns an
    invalid summary.
    """

    name = state.full_name.value if state.full_name else "(not provided)"
    licence = (
        "yes"
        if state.drivers_license and state.drivers_license.value
        else ("no" if state.drivers_license else "not provided")
    )
    area = state.location.matched_name or "not confirmed"
    availability = (
        ", ".join(item.value for item in (state.availability.value if state.availability else []))
        or "not provided"
    )
    schedule = state.preferred_schedule.value.value if state.preferred_schedule else "not provided"
    experience = (
        f"{state.delivery_experience.years:g} years"
        if state.delivery_experience is not None
        else "not provided"
    )
    start = state.start_availability.precision.value if state.start_availability else "not provided"
    result = decision.status.value
    reason = ", ".join(decision.reason_codes) or "none"
    if language is Language.ES:
        return (
            f"Nombre: {name}; licencia vigente: {licence}; zona: {area}; disponibilidad: "
            f"{availability}; horario: {schedule}; experiencia en reparto: {experience}; "
            f"inicio: {start}; resultado determinista: {result} ({reason})."
        )
    return (
        f"Name: {name}; valid licence: {licence}; service area: {area}; availability: "
        f"{availability}; schedule: {schedule}; delivery experience: {experience}; "
        f"start: {start}; deterministic result: {result} ({reason})."
    )


class TurnCoordinator:
    """Coordinate one conversation with idempotency and optimistic versioning."""

    def __init__(
        self,
        uow_factory: _UOWFactory | Callable[[], Any],
        controller: ConversationController,
        interpreter: LanguageInterpreter,
        *,
        summary_generator: SummaryGeneratorProtocol | None = None,
        history_max_pairs: int = 6,
        history_max_characters: int = 8_000,
        max_input_characters: int = 2_000,
        max_turns: int = 40,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least one")
        self.uow_factory = uow_factory
        self.controller = controller
        self.interpreter = interpreter
        self.summary_generator = summary_generator
        self.history_max_pairs = history_max_pairs
        self.history_max_characters = history_max_characters
        self.max_input_characters = max_input_characters
        self.max_turns = max_turns
        self._lock_guard = asyncio.Lock()
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    def _uow(self) -> AbstractAsyncContextManager[SqlAlchemyUnitOfWork]:
        result = self.uow_factory()
        if not hasattr(result, "__aenter__"):
            raise TypeError("uow_factory must return an async context manager")
        return result

    async def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        async with self._lock_guard:
            return self._conversation_locks.setdefault(conversation_id, asyncio.Lock())

    async def create_conversation(
        self,
        *,
        language: Language = Language.ES,
        channel: str = "web",
        candidate_name: str | None = None,
    ) -> ConversationCreated:
        """Create a conversation and return the opaque token exactly once."""

        if len(channel.strip()) > 32:
            raise CoordinatorError("invalid_channel", "channel is invalid", status_code=422)
        token = generate_resume_token()
        now = _utc_now()
        state = ScreeningState.empty(
            language, ruleset_version=self.controller.screening_engine.ruleset_version
        )
        introduction = self.controller.introduction(language)
        async with self._uow() as uow:
            assert (
                uow.candidates
                and uow.sessions
                and uow.conversations
                and uow.messages
                and uow.audit_events
            )
            candidate = CandidateORM(full_name=(candidate_name or None))
            await uow.candidates.add(candidate)
            session = ScreeningSessionORM(
                candidate_id=candidate.id,
                status=ScreeningStatus.IN_PROGRESS.value,
                preferred_language=language.value,
                current_field=state.current_field.value if state.current_field else None,
                screening_state=state.model_dump_json_safe(),
                clarification_counts={},
                ruleset_version=state.ruleset_version,
                version=1,
                started_at=now,
                last_activity_at=now,
            )
            await uow.sessions.add(session)
            conversation = ConversationORM(
                screening_session_id=session.id,
                channel=channel.strip() or "web",
                status=ConversationStatus.ACTIVE.value,
                resume_token_hash=hash_resume_token(token),
                created_at=now,
                updated_at=now,
            )
            await uow.conversations.add(conversation)
            await uow.messages.add(
                MessageORM(
                    conversation_id=conversation.id,
                    direction="assistant",
                    content=introduction,
                    language=language.value,
                    created_at=now,
                )
            )
            await uow.audit_events.add(
                AuditEventORM(
                    screening_session_id=session.id,
                    event_type=AnalyticsEventType.CONVERSATION_STARTED,
                    stage="coordinator",
                    event_metadata={"channel": conversation.channel, "language": language.value},
                    created_at=now,
                )
            )
            await uow.commit()
            return ConversationCreated(
                conversation_id=conversation.id,
                session_id=session.id,
                resume_token=token,
                language=language,
                assistant_message=introduction,
            )

    # A short alias is useful for adapters that call this operation "start".
    start_conversation = create_conversation

    async def verify_resume_token(self, conversation_id: str, token: str) -> bool:
        """Check a bearer token using only its SHA-256 hash."""

        try:
            token_hash = hash_resume_token(token)
        except ValueError:
            return False
        async with self._uow() as uow:
            assert uow.conversations
            conversation = await uow.conversations.get(conversation_id)
            return conversation is not None and secrets.compare_digest(
                conversation.resume_token_hash, token_hash
            )

    async def process_turn(
        self,
        conversation_id: str,
        message: str,
        idempotency_key: str,
        *,
        correlation_id: str | None = None,
        language: Language | None = None,
        input_mode: InteractionMode | str = InteractionMode.TEXT,
    ) -> TurnCoordinatorResult:
        """Process a turn while keeping every model call outside a DB transaction."""

        content = message.strip()
        if not content:
            raise CoordinatorError("empty_message", "message cannot be empty", status_code=422)
        if len(content) > self.max_input_characters:
            raise CoordinatorError(
                "message_too_large", "message exceeds the character limit", status_code=413
            )
        key = idempotency_key.strip()
        if not 1 <= len(key) <= 128:
            raise CoordinatorError(
                "invalid_idempotency_key", "idempotency key is invalid", status_code=422
            )
        try:
            # HTTP callers receive this as a validated enum, but the
            # application service is also used directly by workers and test
            # adapters.  Normalize at this boundary so an invalid value cannot
            # become an AttributeError while reserving a turn.
            input_mode = (
                input_mode
                if isinstance(input_mode, InteractionMode)
                else InteractionMode(input_mode)
            )
        except (TypeError, ValueError) as exc:
            raise CoordinatorError(
                "invalid_input_mode", "input mode is invalid", status_code=422
            ) from exc

        lock = await self._lock_for(conversation_id)
        async with lock:
            guardrail = inspect_message(content)
            try:
                reservation_or_result = await self._reserve_turn(
                    conversation_id,
                    key,
                    content,
                    guardrail.message,
                    input_mode,
                )
            except SQLAlchemyError as exc:
                logger.warning(
                    "turn reservation failed", extra={"conversation_id": conversation_id}
                )
                raise CoordinatorError(
                    "storage_unavailable",
                    "We could not save your message. Please try again.",
                    status_code=503,
                ) from exc
            if isinstance(reservation_or_result, TurnCoordinatorResult):
                return reservation_or_result
            reservation = reservation_or_result

            started = time.monotonic()
            if reservation.turn_limit_reached:
                # The incoming message is still recorded, but once the bound
                # has already been consumed there is no further provider call.
                # A deterministic review handoff keeps this path safe and
                # answerable through idempotent replay.
                interpreted = InterpreterResult(interpretation=TurnInterpretation())
                outcome = self._max_turn_outcome(reservation)
            elif language is not None:
                # A language-selector event is a trusted, explicit preference
                # change.  It is intentionally handled without a provider so
                # the demo remains usable without an API key.
                interpreted = InterpreterResult(
                    interpretation=TurnInterpretation(
                        detected_language=language,
                        language_confidence=1.0,
                        explicit_language=language,
                    )
                )
                outcome = self.controller.process(
                    reservation.state,
                    interpreted.interpretation,
                    now=_utc_now(),
                    message_id=f"turn:{reservation.turn_id}",
                )
            else:
                try:
                    if guardrail.prompt_injection or guardrail.sensitive_data:
                        interpretation = TurnInterpretation(
                            intent=(
                                TurnIntent.PROMPT_INJECTION
                                if guardrail.prompt_injection
                                else TurnIntent.OFF_TOPIC
                            ),
                            prompt_injection_detected=guardrail.prompt_injection,
                            sensitive_data_detected=guardrail.sensitive_data,
                        )
                        interpreted = InterpreterResult(interpretation=interpretation)
                    else:
                        # Closed-vocabulary control replies and explicit
                        # name-prefixed answers do not need an LLM call.
                        # Bare name answers still use the typed interpreter,
                        # then receive a conservative fallback if a provider
                        # returns a valid but empty patch.
                        interpreted = self._deterministic_interpretation(
                            reservation, guardrail.message
                        )
                        if interpreted is None:
                            dependencies = InterpreterDependencies(
                                state=reservation.state,
                                pending_field=reservation.state.current_field,
                                language=reservation.language,
                                now=_utc_now(),
                                local_date=_utc_now().date().isoformat(),
                                correlation_id=correlation_id,
                            )
                            bounded_history = build_bounded_history(
                                reservation.history,
                                max_pairs=self.history_max_pairs,
                                max_characters=self.history_max_characters,
                            )
                            interpreted = await self.interpreter.interpret(
                                guardrail.message,
                                dependencies,
                                message_history=bounded_history,
                            )
                            interpreted = self._recover_unambiguous_confirmation(
                                interpreted, reservation, guardrail.message
                            )
                            interpreted = self._recover_unambiguous_license_answer(
                                interpreted, reservation, guardrail.message
                            )
                            interpreted = self._recover_unambiguous_name_answer(
                                interpreted, reservation, guardrail.message
                            )
                    if not guardrail.prompt_injection and not guardrail.sensitive_data:
                        interpreted = recover_location_answer(
                            reservation.state,
                            guardrail.message,
                            interpreted,
                            service_area_matcher=self.controller.service_area_matcher,
                        )
                    outcome = self.controller.process(
                        reservation.state,
                        interpreted.interpretation,
                        now=_utc_now(),
                        message_id=f"turn:{reservation.turn_id}",
                    )
                except AIProviderError as exc:
                    logger.warning(
                        "turn interpretation provider failure",
                        extra={
                            "conversation_id": conversation_id,
                            "provider_error_category": exc.category,
                        },
                    )
                    return await self._fail_turn(
                        reservation,
                        error_code=(
                            "provider_rate_limited"
                            if exc.category == "rate_limited"
                            else "provider_unavailable"
                        ),
                        message=self._temporary_message(reservation.language),
                        latency_ms=int((time.monotonic() - started) * 1_000),
                    )
                except Exception:
                    logger.exception(
                        "turn interpretation failed", extra={"conversation_id": conversation_id}
                    )
                    return await self._fail_turn(
                        reservation,
                        error_code="provider_unavailable",
                        message=self._temporary_message(reservation.language),
                        latency_ms=int((time.monotonic() - started) * 1_000),
                    )

            # The final allowed turn may be interpreted normally, but an
            # unresolved conversation must not remain open indefinitely.  A
            # terminal fact (qualified/disqualified/opt-out) still wins.
            if reservation.turn_number >= self.max_turns:
                outcome = self._force_max_turn_handoff(outcome)

            try:
                result = await self._commit_outcome(
                    reservation,
                    outcome,
                    interpreted,
                    latency_ms=int((time.monotonic() - started) * 1_000),
                    stored_message=guardrail.message,
                    correlation_id=correlation_id,
                )
            except CoordinatorError:
                raise
            except SQLAlchemyError:
                logger.warning("turn commit failed", extra={"conversation_id": conversation_id})
                try:
                    return await self._fail_turn(
                        reservation,
                        error_code="storage_unavailable",
                        message=self._temporary_message(reservation.language),
                        latency_ms=int((time.monotonic() - started) * 1_000),
                    )
                except SQLAlchemyError as failure:
                    raise CoordinatorError(
                        "storage_unavailable",
                        "We could not save your response. Please try again.",
                        status_code=503,
                    ) from failure

            # Summary generation is intentionally after the state transaction.
            # A slow/failed summary cannot roll back the canonical screening
            # state or hold an open database transaction.
            if result.screening_status in {
                ScreeningStatus.QUALIFIED,
                ScreeningStatus.DISQUALIFIED,
                ScreeningStatus.NEEDS_REVIEW,
            }:
                if result.decision and "max_turns_exceeded" in result.decision.reason_codes:
                    # The turn limit is itself a safety boundary.  Do not
                    # invoke another provider merely to summarize the
                    # deterministic recruiter handoff.
                    summary, summary_status = (
                        _summary_fallback(
                            outcome.state, outcome.decision, outcome.state.preferred_language
                        ),
                        "fallback",
                    )
                else:
                    summary, summary_status = await self._generate_summary(outcome)
                persisted = await self._persist_summary(
                    result.turn_id, reservation.session_id, summary, summary_status
                )
                # Canonical state is already committed.  If the optional
                # summary write is unavailable, expose the pending state so a
                # protected recruiter retry can repair it later.
                result = replace(
                    result,
                    summary_status=summary_status if persisted else "pending",
                )
            return result

    # Compatibility alias used by HTTP adapters and tests.
    handle_turn = process_turn

    def _deterministic_interpretation(
        self, reservation: _Reservation, message: str
    ) -> InterpreterResult | None:
        return interpret_deterministically(
            reservation.state,
            message,
            service_area_matcher=self.controller.service_area_matcher,
        )

    @staticmethod
    def _recover_unambiguous_confirmation(
        interpreted: InterpreterResult,
        reservation: _Reservation,
        message: str,
    ) -> InterpreterResult:
        """Recover an exact yes/no response to a pending proposal.

        Confirmation is a small closed vocabulary owned by the application.
        Recovering an exact one-token answer protects city-area offers and
        correction prompts from a provider that returns a valid schema but
        omits the ``confirmation`` flag.  Longer prose remains model-owned so
        the application never guesses intent from arbitrary text.
        """

        interpretation = interpreted.interpretation
        if (
            reservation.state.pending_confirmation is None
            or interpretation.confirmation is not None
        ):
            return interpreted
        normalized = _normalize_short_message(message)
        value = _CONFIRMATION_VALUES.get(normalized)
        if value is None:
            return interpreted
        return replace(
            interpreted,
            interpretation=interpretation.model_copy(update={"confirmation": value}),
        )

    @staticmethod
    def _recover_unambiguous_license_answer(
        interpreted: InterpreterResult,
        reservation: _Reservation,
        message: str,
    ) -> InterpreterResult:
        """Patch an exact yes/no licence answer if a provider omitted it.

        A short answer to the active boolean prompt is unambiguous and can be
        recovered without guessing from prose.  This is a resilience fallback
        for provider extraction gaps; all other fields remain provider-owned.
        """

        interpretation = interpreted.interpretation
        if (
            reservation.state.current_field is not ScreeningField.DRIVERS_LICENSE
            or interpretation.drivers_license is not None
        ):
            return interpreted
        normalized = _normalize_short_message(message)
        value = _LICENSE_VALUES.get(normalized)
        if value is None:
            return interpreted
        patched = interpretation.model_copy(
            update={
                "drivers_license": ExtractedValue(
                    value=value, provided=True, evidence=message[:500]
                )
            }
        )
        return replace(interpreted, interpretation=patched)

    @staticmethod
    def _recover_unambiguous_name_answer(
        interpreted: InterpreterResult,
        reservation: _Reservation,
        message: str,
    ) -> InterpreterResult:
        """Recover a clear bare name when the model returns an empty patch.

        ``TurnInterpretation`` intentionally permits an empty patch for
        question-only/off-topic turns.  Some providers can therefore return
        a valid default object for a plainly answered name prompt.  Apply the
        conservative name parser only when the model supplied no other fact;
        all multi-field, correction, and ambiguous messages remain model-owned.
        """

        interpretation = interpreted.interpretation
        if reservation.state.current_field is not ScreeningField.FULL_NAME:
            return interpreted
        if interpretation.intent not in {TurnIntent.ANSWER, TurnIntent.UNKNOWN}:
            return interpreted
        if interpretation.full_name is not None and (
            interpretation.full_name.provided
            or interpretation.full_name.ambiguous
            or interpretation.full_name.correction
        ):
            return interpreted
        if (
            any(
                value is not None
                for value in (
                    interpretation.drivers_license,
                    interpretation.location,
                    interpretation.availability,
                    interpretation.preferred_schedule,
                    interpretation.delivery_experience,
                    interpretation.start_availability,
                )
            )
            or interpretation.candidate_questions
        ):
            return interpreted
        name = _extract_simple_full_name(message)
        if name is None:
            return interpreted
        patched = interpretation.model_copy(
            update={
                # This repairs the legacy state where the name prompt was
                # already shown but the disclosure flag was not persisted.
                "disclosure_acknowledged": True,
                "full_name": ExtractedValue(
                    value=name,
                    provided=True,
                    evidence=message[:500],
                ),
            }
        )
        usage = dict(interpreted.usage)
        usage["deterministic_fallback"] = True
        usage["deterministic_fallback_field"] = ScreeningField.FULL_NAME.value
        return replace(interpreted, interpretation=patched, usage=usage)

    async def _reserve_turn(
        self,
        conversation_id: str,
        idempotency_key: str,
        original_message: str,
        stored_message: str,
        input_mode: InteractionMode,
    ) -> _Reservation | TurnCoordinatorResult:
        request_hash = _request_hash(original_message)
        async with self._uow() as uow:
            assert (
                uow.conversations
                and uow.sessions
                and uow.turns
                and uow.messages
                and uow.audit_events
            )
            conversation = await uow.conversations.get(conversation_id)
            if conversation is None:
                raise CoordinatorError(
                    "conversation_not_found", "conversation was not found", status_code=404
                )
            existing = await uow.turns.get_by_idempotency(conversation_id, idempotency_key)
            if existing is not None:
                if existing.request_hash and existing.request_hash != request_hash:
                    raise CoordinatorError(
                        "idempotency_key_reused",
                        "idempotency key was already used for another message",
                        status_code=409,
                    )
                if existing.status == TurnStatus.COMPLETED.value:
                    return await self._replay_result(uow, existing, conversation_id)
                if existing.status == TurnStatus.FAILED.value:
                    return await self._replay_result(uow, existing, conversation_id)
                raise CoordinatorError(
                    "turn_in_progress",
                    "another request with this idempotency key is still processing",
                    status_code=409,
                )
            if conversation.status != ConversationStatus.ACTIVE.value:
                raise CoordinatorError(
                    "conversation_closed", "this conversation is closed", status_code=409
                )
            session = await uow.sessions.get(conversation.screening_session_id)
            if session is None:
                raise CoordinatorError(
                    "session_not_found", "screening session was not found", status_code=404
                )
            state = self._state_from_session(session)
            turn_count = await uow.turns.count_for_conversation(conversation_id)
            rows = await uow.messages.list_for_conversation(conversation_id, limit=400)
            history = tuple(
                HistoryMessage(
                    role="user" if row.direction == "user" else "assistant",
                    content=row.content,
                    created_at=row.created_at,
                )
                for row in rows
            )
            now = _utc_now()
            turn = TurnORM(
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status=TurnStatus.PROCESSING.value,
                input_mode=input_mode.value,
                state_version_before=session.version,
                created_at=now,
            )
            await uow.turns.add(turn)
            await uow.messages.add(
                MessageORM(
                    conversation_id=conversation_id,
                    turn_id=turn.id,
                    direction="user",
                    content=stored_message,
                    language=state.preferred_language.value,
                    created_at=now,
                )
            )
            await uow.audit_events.add(
                AuditEventORM(
                    screening_session_id=session.id,
                    turn_id=turn.id,
                    event_type=AnalyticsEventType.TURN_STARTED,
                    stage="coordinator",
                    event_metadata={
                        "idempotency_key": idempotency_key,
                        "input_mode": input_mode.value,
                    },
                    created_at=now,
                )
            )
            await uow.commit()
            return _Reservation(
                conversation_id=conversation_id,
                session_id=session.id,
                turn_id=turn.id,
                request_hash=request_hash,
                state=state,
                version=session.version,
                language=state.preferred_language,
                history=history,
                stored_message=stored_message,
                turn_number=turn_count + 1,
                turn_limit_reached=turn_count >= self.max_turns,
            )

    async def _replay_result(
        self,
        uow: SqlAlchemyUnitOfWork,
        turn: TurnORM,
        conversation_id: str,
    ) -> TurnCoordinatorResult:
        payload = turn.response_payload or {}
        if payload:
            decision_payload = payload.get("decision")
            decision = (
                ScreeningDecision.model_validate(decision_payload)
                if isinstance(decision_payload, Mapping)
                else None
            )
            return TurnCoordinatorResult(
                conversation_id=conversation_id,
                turn_id=turn.id,
                status=TurnStatus(turn.status),
                screening_status=_as_status(
                    payload.get("screening_status", ScreeningStatus.IN_PROGRESS.value)
                ),
                assistant_message=str(
                    payload.get("assistant_message") or turn.response_message or ""
                ),
                state_version=int(
                    payload.get(
                        "state_version", turn.state_version_after or turn.state_version_before or 1
                    )
                ),
                next_field=(str(payload["next_field"]) if payload.get("next_field") else None),
                decision=decision,
                idempotent=True,
                error_code=turn.error_code,
                summary_status=(
                    str(payload["summary_status"]) if payload.get("summary_status") else None
                ),
            )
        assert uow.messages and uow.sessions and uow.conversations
        rows = await uow.messages.list_for_conversation(conversation_id, limit=400)
        assistant = next(
            (
                row.content
                for row in reversed(rows)
                if row.turn_id == turn.id and row.direction == "assistant"
            ),
            None,
        )
        conversation = await uow.conversations.get(conversation_id)
        if conversation is None:
            raise CoordinatorError(
                "conversation_not_found", "conversation was not found", status_code=404
            )
        session = await uow.sessions.get(conversation.screening_session_id)
        status = _as_status(session.status if session else ScreeningStatus.IN_PROGRESS.value)
        return TurnCoordinatorResult(
            conversation_id=conversation_id,
            turn_id=turn.id,
            status=TurnStatus(turn.status),
            screening_status=status,
            assistant_message=assistant or turn.response_message or "",
            state_version=turn.state_version_after or turn.state_version_before or 1,
            idempotent=True,
            error_code=turn.error_code,
        )

    async def _commit_outcome(
        self,
        reservation: _Reservation,
        outcome: ConversationTurnResult,
        interpreted: InterpreterResult,
        *,
        latency_ms: int,
        stored_message: str,
        correlation_id: str | None,
    ) -> TurnCoordinatorResult:
        decision = outcome.decision
        after_status = decision.status
        summary_status: str | None = (
            "pending"
            if after_status
            in {
                ScreeningStatus.QUALIFIED,
                ScreeningStatus.DISQUALIFIED,
                ScreeningStatus.NEEDS_REVIEW,
            }
            else None
        )
        result_payload: dict[str, Any] = {
            "screening_status": after_status.value,
            "assistant_message": outcome.assistant_message,
            "next_field": outcome.next_field.value if outcome.next_field else None,
            "state_version": reservation.version + 1,
            "decision": decision.model_dump(mode="json"),
            "summary_status": summary_status,
        }
        async with self._uow() as uow:
            assert (
                uow.candidates
                and uow.sessions
                and uow.turns
                and uow.messages
                and uow.results
                and uow.audit_events
                and uow.conversations
            )
            session = await uow.sessions.get(reservation.session_id)
            turn = await uow.turns.get(reservation.turn_id)
            conversation = await uow.conversations.get(reservation.conversation_id)
            if session is None or turn is None or conversation is None:
                raise CoordinatorError(
                    "storage_unavailable",
                    "We could not save your response. Please try again.",
                    status_code=503,
                )
            try:
                assert_transition(ScreeningStatus(session.status), after_status)
            except (ValueError, KeyError) as exc:
                # Do not leave the reserved turn in ``processing`` when a
                # malformed controller result or stale terminal state reaches
                # this boundary.  The transition table is the final write
                # guard, and the failure remains safely replayable.
                failure_message = self._temporary_message(reservation.language)
                await self._mark_turn_failed_in_uow(
                    uow,
                    turn,
                    error_code="invalid_status_transition",
                    response_message=failure_message,
                    latency_ms=latency_ms,
                )
                await uow.messages.add(
                    MessageORM(
                        conversation_id=reservation.conversation_id,
                        turn_id=turn.id,
                        direction="assistant",
                        content=failure_message,
                        language=reservation.language.value,
                        created_at=_utc_now(),
                    )
                )
                await uow.audit_events.add(
                    AuditEventORM(
                        screening_session_id=reservation.session_id,
                        turn_id=turn.id,
                        event_type=AnalyticsEventType.TURN_FAILED,
                        stage="coordinator",
                        event_metadata={
                            "error_code": "invalid_status_transition",
                            "input_mode": turn.input_mode,
                        },
                        created_at=_utc_now(),
                    )
                )
                await uow.commit()
                raise CoordinatorError(
                    "invalid_status_transition",
                    "Your conversation could not continue safely; a recruiter can review it.",
                    status_code=409,
                ) from exc
            if session.version != reservation.version:
                await self._mark_turn_failed_in_uow(
                    uow,
                    turn,
                    error_code="concurrency_conflict",
                    response_message=self._temporary_message(reservation.language),
                    latency_ms=latency_ms,
                )
                await uow.audit_events.add(
                    AuditEventORM(
                        screening_session_id=reservation.session_id,
                        turn_id=reservation.turn_id,
                        event_type=AnalyticsEventType.TURN_FAILED,
                        stage="coordinator",
                        event_metadata={
                            "error_code": "concurrency_conflict",
                            "input_mode": turn.input_mode,
                        },
                        created_at=_utc_now(),
                    )
                )
                await uow.commit()
                return TurnCoordinatorResult(
                    reservation.conversation_id,
                    reservation.turn_id,
                    TurnStatus.FAILED,
                    _as_status(session.status),
                    self._temporary_message(reservation.language),
                    session.version,
                    idempotent=False,
                    error_code="concurrency_conflict",
                )
            saved = await uow.sessions.save_state(
                reservation.session_id,
                state=outcome.state.model_dump_json_safe(),
                status=after_status.value,
                expected_version=reservation.version,
                current_field=outcome.state.current_field.value
                if outcome.state.current_field
                else None,
                clarification_counts={
                    field.value: count
                    for field, count in outcome.state.clarification_counts.items()
                },
                last_activity_at=_utc_now(),
                completed_at=(
                    _utc_now()
                    if after_status
                    in {
                        ScreeningStatus.QUALIFIED,
                        ScreeningStatus.DISQUALIFIED,
                        ScreeningStatus.NEEDS_REVIEW,
                        ScreeningStatus.ABANDONED,
                    }
                    else None
                ),
            )
            if saved is None:
                raise CoordinatorError(
                    "concurrency_conflict",
                    "Your conversation changed; please retry.",
                    status_code=409,
                )
            candidate = await uow.candidates.get(session.candidate_id)
            if candidate is None:
                raise CoordinatorError(
                    "storage_unavailable",
                    "We could not save your response. Please try again.",
                    status_code=503,
                )
            # ``candidate_name`` at conversation creation is a display hint;
            # once canonical screening state has a name it is the source of
            # truth for recruiter-facing candidate data.
            if outcome.state.full_name is not None:
                candidate.full_name = outcome.state.full_name.value
            if (
                outcome.state.disclosure_acknowledged
                and conversation.disclosure_acknowledged_at is None
            ):
                conversation.disclosure_acknowledged_at = _utc_now()
            turn.status = TurnStatus.COMPLETED.value
            turn.state_version_after = reservation.version + 1
            turn.response_message = outcome.assistant_message[:2_000]
            turn.response_payload = result_payload
            turn.model_name = interpreted.model_name
            turn.model_usage = interpreted.usage or None
            turn.latency_ms = max(0, latency_ms)
            turn.completed_at = _utc_now()
            await uow.messages.add(
                MessageORM(
                    conversation_id=reservation.conversation_id,
                    turn_id=turn.id,
                    direction="assistant",
                    content=outcome.assistant_message[:2_000],
                    language=outcome.state.preferred_language.value,
                    created_at=_utc_now(),
                )
            )
            existing_result = await uow.results.get_for_session(reservation.session_id)
            if existing_result is None:
                existing_result = ScreeningResultORM(
                    screening_session_id=reservation.session_id,
                    status=after_status.value,
                    reason_codes=decision.reason_codes,
                    rule_trace=decision.rule_trace,
                    summary_status=summary_status or "not_applicable",
                    handoff_status=(
                        "ready"
                        if after_status in {ScreeningStatus.QUALIFIED, ScreeningStatus.NEEDS_REVIEW}
                        else "not_applicable"
                    ),
                )
                await uow.results.add(existing_result)
            else:
                existing_result.status = after_status.value
                existing_result.reason_codes = decision.reason_codes
                existing_result.rule_trace = decision.rule_trace
                existing_result.summary_status = summary_status or "not_applicable"
                existing_result.handoff_status = (
                    "ready"
                    if after_status in {ScreeningStatus.QUALIFIED, ScreeningStatus.NEEDS_REVIEW}
                    else "not_applicable"
                )
            conversation.status = (
                ConversationStatus.OPTED_OUT.value
                if after_status is ScreeningStatus.ABANDONED
                else ConversationStatus.COMPLETED.value
                if after_status
                in {
                    ScreeningStatus.QUALIFIED,
                    ScreeningStatus.DISQUALIFIED,
                    ScreeningStatus.NEEDS_REVIEW,
                }
                else ConversationStatus.ACTIVE.value
            )
            await uow.audit_events.add(
                AuditEventORM(
                    screening_session_id=reservation.session_id,
                    turn_id=turn.id,
                    event_type=(
                        "guardrail_blocked" if outcome.security_event else "turn_completed"
                    ),
                    stage="coordinator",
                    event_metadata={
                        "correlation_id": correlation_id,
                        "changed_fields": [field.value for field in outcome.changed_fields],
                        "security_event": outcome.security_event,
                        "decision_status": after_status.value,
                        "faq_answered": outcome.faq_answered,
                        "input_mode": turn.input_mode,
                    },
                    created_at=_utc_now(),
                )
            )
            if outcome.faq_answered:
                await uow.audit_events.add(
                    AuditEventORM(
                        screening_session_id=reservation.session_id,
                        turn_id=turn.id,
                        event_type=AnalyticsEventType.FAQ_ANSWERED,
                        stage="conversation",
                        event_metadata={},
                        created_at=_utc_now(),
                    )
                )
            terminal_event = {
                ScreeningStatus.QUALIFIED: AnalyticsEventType.SCREENING_QUALIFIED,
                ScreeningStatus.DISQUALIFIED: AnalyticsEventType.SCREENING_DISQUALIFIED,
                ScreeningStatus.NEEDS_REVIEW: AnalyticsEventType.SCREENING_NEEDS_REVIEW,
                ScreeningStatus.ABANDONED: AnalyticsEventType.CANDIDATE_OPTED_OUT,
            }.get(after_status)
            if terminal_event is not None:
                await uow.audit_events.add(
                    AuditEventORM(
                        screening_session_id=reservation.session_id,
                        turn_id=turn.id,
                        event_type=terminal_event,
                        stage="coordinator",
                        event_metadata={"reason_codes": decision.reason_codes},
                        created_at=_utc_now(),
                    )
                )
            await uow.commit()
            return TurnCoordinatorResult(
                conversation_id=reservation.conversation_id,
                turn_id=turn.id,
                status=TurnStatus.COMPLETED,
                screening_status=after_status,
                assistant_message=outcome.assistant_message,
                state_version=reservation.version + 1,
                next_field=outcome.next_field.value if outcome.next_field else None,
                decision=decision,
                summary_status=summary_status,
            )

    def _max_turn_decision(
        self, state: ScreeningState, *, base: ScreeningDecision | None = None
    ) -> ScreeningDecision:
        """Build the deterministic recruiter-handoff decision at the turn bound."""

        existing = base or self.controller.screening_engine.evaluate(state)
        return existing.model_copy(
            update={
                "status": ScreeningStatus.NEEDS_REVIEW,
                "reason_codes": ["max_turns_exceeded"],
                "rule_trace": {
                    **existing.rule_trace,
                    "decision_rule": "max_turns_exceeded",
                    "max_turns": self.max_turns,
                },
            }
        )

    def _max_turn_outcome(self, reservation: _Reservation) -> ConversationTurnResult:
        decision = self._max_turn_decision(reservation.state)
        return ConversationTurnResult(
            state=reservation.state,
            decision=decision,
            assistant_message=self._max_turn_message(reservation.language),
            changed_fields=[],
        )

    def _force_max_turn_handoff(self, outcome: ConversationTurnResult) -> ConversationTurnResult:
        """Close an unresolved conversation on its final allowed turn."""

        if outcome.decision.status is not ScreeningStatus.IN_PROGRESS:
            return outcome
        return replace(
            outcome,
            decision=self._max_turn_decision(outcome.state, base=outcome.decision),
            assistant_message=self._max_turn_message(outcome.state.preferred_language),
            next_field=None,
        )

    async def _generate_summary(self, outcome: ConversationTurnResult) -> tuple[str, str]:
        language = outcome.state.preferred_language
        fallback = _summary_fallback(outcome.state, outcome.decision, language)
        if self.summary_generator is None:
            return fallback, "fallback"
        try:
            generated = await self.summary_generator.generate(
                outcome.state,
                outcome.decision,
                language=language,
            )
            summary = generated.summary
            if summary_is_safe(summary):
                return summary, "generated"
        except Exception:
            logger.exception("summary generation failed")
        return fallback, "fallback"

    async def _persist_summary(
        self,
        turn_id: str | None,
        session_id: str,
        summary: str,
        summary_status: str,
    ) -> bool:
        """Persist an optional recruiter summary without failing the turn.

        The screening decision and conversation state are committed first. A
        summary is derived data, so a transient database/provider problem must
        leave the candidate with a successful canonical response and a
        ``pending`` status rather than turning a completed turn into a 500.
        """

        try:
            async with self._uow() as uow:
                assert uow.results and uow.turns and uow.audit_events
                result = await uow.results.get_for_session(session_id)
                turn = await uow.turns.get(turn_id) if turn_id else None
                if result is not None:
                    result.summary = summary[:1_200]
                    result.summary_status = summary_status
                if turn is not None:
                    payload = dict(turn.response_payload or {})
                    payload["summary_status"] = summary_status
                    turn.response_payload = payload
                if result is not None:
                    event_type = (
                        AnalyticsEventType.SUMMARY_GENERATED
                        if summary_status == "generated"
                        else AnalyticsEventType.SUMMARY_FALLBACK
                    )
                    await uow.audit_events.add(
                        AuditEventORM(
                            screening_session_id=session_id,
                            turn_id=turn_id,
                            event_type=event_type,
                            stage="summary",
                            event_metadata={"summary_status": summary_status},
                            created_at=_utc_now(),
                        )
                    )
                await uow.commit()
        except Exception as exc:
            # The JSON formatter hashes identifiers and drops exception text;
            # retain only the exception class for diagnostics.
            logger.warning(
                "summary persistence failed",
                extra={
                    "event": "summary_persistence_failed",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "error_type": type(exc).__name__,
                    "summary_status": summary_status,
                },
            )
            return False
        return True

    async def retry_summary(self, session_id: str) -> ScreeningView:
        """Regenerate and persist a terminal screening summary.

        This operation is intentionally internal-only at the HTTP layer. It
        does not reopen or re-evaluate a screening; it only repairs derived
        recruiter-facing text for an already committed terminal result.
        """

        async with self._uow() as uow:
            assert uow.sessions and uow.results and uow.conversations and uow.turns
            session = await uow.sessions.get(session_id)
            if session is None:
                raise CoordinatorError(
                    "screening_not_found", "screening was not found", status_code=404
                )
            screening_status = _as_status(session.status)
            if screening_status not in {
                ScreeningStatus.QUALIFIED,
                ScreeningStatus.DISQUALIFIED,
                ScreeningStatus.NEEDS_REVIEW,
            }:
                raise CoordinatorError(
                    "summary_not_ready",
                    "a summary can only be retried for a terminal screening",
                    status_code=409,
                )
            result = await uow.results.get_for_session(session_id)
            if result is None:
                raise CoordinatorError(
                    "result_not_found", "screening result was not found", status_code=404
                )
            conversation = await uow.conversations.get_by_session(session_id)
            if conversation is None:
                raise CoordinatorError(
                    "conversation_not_found", "conversation was not found", status_code=404
                )
            latest_turn = await uow.turns.latest_for_conversation(conversation.id)
            turn_id = latest_turn.id if latest_turn is not None else None
            state = self._state_from_session(session)
            decision = self._decision_from_result(result)
            if decision is None:
                raise CoordinatorError(
                    "result_not_found", "screening decision was not found", status_code=404
                )

        outcome = ConversationTurnResult(
            state=state,
            decision=decision,
            assistant_message="",
        )
        summary, summary_status = await self._generate_summary(outcome)
        await self._persist_summary(turn_id, session_id, summary, summary_status)
        return await self.get_screening(session_id)

    async def _fail_turn(
        self,
        reservation: _Reservation,
        *,
        error_code: str,
        message: str,
        latency_ms: int,
    ) -> TurnCoordinatorResult:
        async with self._uow() as uow:
            assert uow.turns and uow.messages and uow.audit_events
            turn = await uow.turns.get(reservation.turn_id)
            if turn is None:
                raise CoordinatorError(
                    "storage_unavailable",
                    "We could not save your response. Please try again.",
                    status_code=503,
                )
            await self._mark_turn_failed_in_uow(
                uow,
                turn,
                error_code=error_code,
                response_message=message,
                latency_ms=latency_ms,
            )
            await uow.messages.add(
                MessageORM(
                    conversation_id=reservation.conversation_id,
                    turn_id=turn.id,
                    direction="assistant",
                    content=message,
                    language=reservation.language.value,
                    created_at=_utc_now(),
                )
            )
            await uow.audit_events.add(
                AuditEventORM(
                    screening_session_id=reservation.session_id,
                    turn_id=turn.id,
                    event_type=AnalyticsEventType.TURN_FAILED,
                    stage="coordinator",
                    event_metadata={
                        "error_code": error_code,
                        "input_mode": turn.input_mode,
                    },
                    created_at=_utc_now(),
                )
            )
            await uow.commit()
        return TurnCoordinatorResult(
            conversation_id=reservation.conversation_id,
            turn_id=reservation.turn_id,
            status=TurnStatus.FAILED,
            screening_status=ScreeningStatus.IN_PROGRESS,
            assistant_message=message,
            state_version=reservation.version,
            error_code=error_code,
        )

    @staticmethod
    async def _mark_turn_failed_in_uow(
        uow: SqlAlchemyUnitOfWork,
        turn: TurnORM,
        *,
        error_code: str,
        response_message: str,
        latency_ms: int,
    ) -> None:
        turn.status = TurnStatus.FAILED.value
        turn.error_code = error_code[:100]
        turn.response_message = response_message[:2_000]
        turn.response_payload = {
            "screening_status": ScreeningStatus.IN_PROGRESS.value,
            "assistant_message": response_message[:2_000],
            "state_version": turn.state_version_before or 1,
            "summary_status": None,
        }
        turn.latency_ms = max(0, latency_ms)
        turn.completed_at = _utc_now()

    @staticmethod
    def _temporary_message(language: Language) -> str:
        return (
            "I’m sorry, I’m temporarily unable to process that message. Your previous information is unchanged; please try again."
            if language is Language.EN
            else "Lo siento, no puedo procesar ese mensaje temporalmente. Tu información anterior no ha cambiado; inténtalo de nuevo."
        )

    @staticmethod
    def _max_turn_message(language: Language) -> str:
        return (
            "We have reached the maximum number of screening messages. A recruiter will review your application and follow up."
            if language is Language.EN
            else "Hemos alcanzado el número máximo de mensajes de la evaluación. Una persona reclutadora revisará tu solicitud y te contactará."
        )

    @staticmethod
    def _state_from_session(session: ScreeningSessionORM) -> ScreeningState:
        try:
            return ScreeningState.model_validate(session.screening_state)
        except Exception as exc:
            raise CoordinatorError(
                "invalid_persisted_state", "screening state is unavailable", status_code=503
            ) from exc

    async def get_conversation(
        self, conversation_id: str, *, include_messages: bool = True
    ) -> ConversationView:
        async with self._uow() as uow:
            assert uow.conversations and uow.sessions and uow.messages and uow.results
            conversation = await uow.conversations.get(conversation_id)
            if conversation is None:
                raise CoordinatorError(
                    "conversation_not_found", "conversation was not found", status_code=404
                )
            session = await uow.sessions.get(conversation.screening_session_id)
            if session is None:
                raise CoordinatorError(
                    "session_not_found", "screening session was not found", status_code=404
                )
            state = self._state_from_session(session)
            result = await uow.results.get_for_session(session.id)
            messages: tuple[dict[str, Any], ...] = ()
            if include_messages:
                rows = await uow.messages.list_for_conversation(conversation_id, limit=400)
                messages = tuple(
                    {
                        "id": row.id,
                        "direction": row.direction,
                        "content": row.content,
                        "language": row.language,
                        "created_at": row.created_at,
                    }
                    for row in rows
                )
            decision = self._decision_from_result(result)
            return ConversationView(
                conversation_id=conversation.id,
                session_id=session.id,
                status=ConversationStatus(conversation.status),
                language=state.preferred_language,
                state=state,
                state_version=session.version,
                decision=decision,
                messages=messages,
            )

    async def list_screenings(
        self, *, status: ScreeningStatus | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[ScreeningView, ...]:
        async with self._uow() as uow:
            assert uow.sessions and uow.candidates and uow.conversations and uow.results
            sessions = await uow.sessions.list(
                status=status.value if status else None, limit=limit, offset=offset
            )
            views: list[ScreeningView] = []
            for session in sessions:
                candidate = await uow.candidates.get(session.candidate_id)
                conversation = (
                    await uow.conversations.get_by_session(session.id)
                    if hasattr(uow.conversations, "get_by_session")
                    else None
                )
                if conversation is None:
                    # Older repository implementations can still serve data by
                    # querying the conversation id through a helper below.
                    conversation = await self._conversation_for_session(uow, session.id)
                if conversation is None:
                    continue
                result = await uow.results.get_for_session(session.id)
                views.append(self._screening_view(session, candidate, conversation, result, ()))
            return tuple(views)

    async def get_screening(
        self, session_id: str, *, include_messages: bool = True
    ) -> ScreeningView:
        async with self._uow() as uow:
            assert uow.sessions and uow.candidates and uow.results
            session = await uow.sessions.get(session_id)
            if session is None:
                raise CoordinatorError(
                    "screening_not_found", "screening was not found", status_code=404
                )
            candidate = await uow.candidates.get(session.candidate_id)
            conversation = await self._conversation_for_session(uow, session.id)
            if conversation is None:
                raise CoordinatorError(
                    "conversation_not_found", "conversation was not found", status_code=404
                )
            rows = (
                await uow.messages.list_for_conversation(conversation.id, limit=400)
                if include_messages and uow.messages
                else ()
            )
            messages = tuple(
                {
                    "id": row.id,
                    "direction": row.direction,
                    "content": row.content,
                    "language": row.language,
                    "created_at": row.created_at,
                }
                for row in rows
            )
            result = await uow.results.get_for_session(session.id)
            return self._screening_view(session, candidate, conversation, result, messages)

    async def get_analytics(self) -> AnalyticsView:
        """Return aggregate counters used by an internal operations dashboard."""

        async with self._uow() as uow:
            assert uow.session is not None
            status_rows = await uow.session.execute(
                select(ScreeningSessionORM.status, func.count(ScreeningSessionORM.id)).group_by(
                    ScreeningSessionORM.status
                )
            )
            status_counts = {str(status): int(count) for status, count in status_rows.all()}
            for screening_status in ScreeningStatus:
                status_counts.setdefault(screening_status.value, 0)

            total_turns = int((await uow.session.scalar(select(func.count(TurnORM.id)))) or 0)
            completed_turns = int(
                (
                    await uow.session.scalar(
                        select(func.count(TurnORM.id)).where(
                            TurnORM.status == TurnStatus.COMPLETED.value
                        )
                    )
                )
                or 0
            )
            failed_turns = int(
                (
                    await uow.session.scalar(
                        select(func.count(TurnORM.id)).where(
                            TurnORM.status == TurnStatus.FAILED.value
                        )
                    )
                )
                or 0
            )
            average_latency = await uow.session.scalar(select(func.avg(TurnORM.latency_ms)))
            summaries_generated = int(
                (
                    await uow.session.scalar(
                        select(func.count(ScreeningResultORM.id)).where(
                            ScreeningResultORM.summary_status == "generated"
                        )
                    )
                )
                or 0
            )
            summaries_fallback = int(
                (
                    await uow.session.scalar(
                        select(func.count(ScreeningResultORM.id)).where(
                            ScreeningResultORM.summary_status == "fallback"
                        )
                    )
                )
                or 0
            )
            handoffs_ready = int(
                (
                    await uow.session.scalar(
                        select(func.count(ScreeningResultORM.id)).where(
                            ScreeningResultORM.handoff_status == "ready"
                        )
                    )
                )
                or 0
            )
            reviews_recorded = int(
                (await uow.session.scalar(select(func.count(RecruiterReviewORM.id)))) or 0
            )
            event_rows = await uow.session.scalars(select(AuditEventORM))
            persisted_event_rows = tuple(event_rows.all())
            event_aggregate = aggregate_events(
                AnalyticsEvent.from_row(row) for row in persisted_event_rows
            )
            session_rows = tuple((await uow.session.scalars(select(ScreeningSessionORM))).all())
            conversation_rows = tuple((await uow.session.scalars(select(ConversationORM))).all())
            turn_rows = tuple((await uow.session.scalars(select(TurnORM))).all())
            message_rows = tuple((await uow.session.scalars(select(MessageORM))).all())
            result_rows = tuple((await uow.session.scalars(select(ScreeningResultORM))).all())
            persisted_aggregate = aggregate_persisted_data(
                session_rows,
                conversation_rows,
                turn_rows,
                message_rows,
                result_rows,
                persisted_event_rows,
            )
            summaries_pending = int(
                (
                    await uow.session.scalar(
                        select(func.count(ScreeningResultORM.id)).where(
                            ScreeningResultORM.summary_status == "pending"
                        )
                    )
                )
                or 0
            )
            screenings_completed = sum(
                status_counts.get(status.value, 0)
                for status in (
                    ScreeningStatus.QUALIFIED,
                    ScreeningStatus.DISQUALIFIED,
                    ScreeningStatus.NEEDS_REVIEW,
                )
            )
            screenings_started = event_aggregate.screenings_started or sum(status_counts.values())
            return AnalyticsView(
                total_screenings=sum(status_counts.values()),
                status_counts=status_counts,
                total_turns=total_turns,
                completed_turns=completed_turns,
                failed_turns=failed_turns,
                average_turn_latency_ms=(
                    float(average_latency) if average_latency is not None else None
                ),
                summaries_generated=summaries_generated,
                summaries_fallback=summaries_fallback,
                handoffs_ready=handoffs_ready,
                reviews_recorded=reviews_recorded,
                total_events=event_aggregate.total_events,
                event_counts=event_aggregate.event_counts,
                guardrail_blocks=event_aggregate.guardrail_blocks,
                reengagements_sent=event_aggregate.reengagements_sent,
                reengagements_suppressed=event_aggregate.reengagements_suppressed,
                screenings_started=screenings_started,
                screenings_completed=screenings_completed,
                screenings_in_progress=status_counts[ScreeningStatus.IN_PROGRESS.value],
                screenings_abandoned=status_counts[ScreeningStatus.ABANDONED.value],
                turns_started=event_aggregate.turns_started,
                qualified=status_counts[ScreeningStatus.QUALIFIED.value],
                disqualified=status_counts[ScreeningStatus.DISQUALIFIED.value],
                needs_review=status_counts[ScreeningStatus.NEEDS_REVIEW.value],
                opted_out=event_aggregate.opted_out,
                summaries_pending=summaries_pending,
                completion_rate=(
                    screenings_completed / screenings_started if screenings_started else None
                ),
                average_completed_messages=persisted_aggregate.average_completed_messages,
                average_completed_turns=persisted_aggregate.average_completed_turns,
                average_screening_duration_seconds=(
                    persisted_aggregate.average_screening_duration_seconds
                ),
                language_distribution=persisted_aggregate.language_distribution,
                faq_usage_count=persisted_aggregate.faq_usage_count,
                clarification_retry_count=persisted_aggregate.clarification_retry_count,
                clarification_retry_counts=persisted_aggregate.clarification_retry_counts,
                disqualification_reason_distribution=(
                    persisted_aggregate.disqualification_reason_distribution
                ),
                dropoff_stage_distribution=persisted_aggregate.dropoff_stage_distribution,
                interaction_mode_distribution=(persisted_aggregate.interaction_mode_distribution),
            )

    async def record_review(
        self, session_id: str, reviewer_id: str, decision: str, notes: str | None = None
    ) -> dict[str, Any]:
        if not reviewer_id.strip() or len(reviewer_id) > 200:
            raise CoordinatorError("invalid_reviewer", "reviewer is invalid", status_code=422)
        if decision not in {"advance", "reject", "needs_review", "qualified", "disqualified"}:
            raise CoordinatorError(
                "invalid_review_decision", "review decision is invalid", status_code=422
            )
        if notes is not None and len(notes) > 2_000:
            raise CoordinatorError("notes_too_large", "review notes are too long", status_code=422)
        async with self._uow() as uow:
            assert uow.sessions and uow.results and uow.reviews and uow.audit_events
            session = await uow.sessions.get(session_id)
            if session is None:
                raise CoordinatorError(
                    "screening_not_found", "screening was not found", status_code=404
                )
            result = await uow.results.get_for_session(session_id)
            if result is None:
                raise CoordinatorError(
                    "result_not_found", "screening result was not found", status_code=404
                )
            review = RecruiterReviewORM(
                screening_result_id=result.id,
                reviewer_id=reviewer_id.strip(),
                decision=decision,
                notes=notes.strip() if notes else None,
            )
            await uow.reviews.add(review)
            result.handoff_status = "reviewed"
            await uow.audit_events.add(
                AuditEventORM(
                    screening_session_id=session_id,
                    event_type="recruiter_review_recorded",
                    stage="internal_api",
                    event_metadata={"reviewer_id": reviewer_id.strip(), "decision": decision},
                    created_at=_utc_now(),
                )
            )
            await uow.commit()
            return {
                "id": review.id,
                "screening_session_id": session_id,
                "reviewer_id": review.reviewer_id,
                "decision": review.decision,
                "notes": review.notes,
                "created_at": review.created_at,
            }

    @staticmethod
    async def _conversation_for_session(
        uow: SqlAlchemyUnitOfWork, session_id: str
    ) -> ConversationORM | None:
        from sqlalchemy import select

        assert uow.session is not None
        return await uow.session.scalar(
            select(ConversationORM).where(ConversationORM.screening_session_id == session_id)
        )

    @staticmethod
    def _decision_from_result(result: ScreeningResultORM | None) -> ScreeningDecision | None:
        if result is None:
            return None
        return ScreeningDecision(
            status=_as_status(result.status),
            reason_codes=list(result.reason_codes or []),
            rule_trace=dict(result.rule_trace or {}),
        )

    def _screening_view(
        self,
        session: ScreeningSessionORM,
        candidate: CandidateORM | None,
        conversation: ConversationORM,
        result: ScreeningResultORM | None,
        messages: Sequence[dict[str, Any]],
    ) -> ScreeningView:
        return ScreeningView(
            session_id=session.id,
            conversation_id=conversation.id,
            candidate_id=session.candidate_id,
            candidate_name=candidate.full_name if candidate else None,
            status=_as_status(session.status),
            state=self._state_from_session(session),
            state_version=session.version,
            decision=self._decision_from_result(result),
            summary=result.summary if result else None,
            summary_status=result.summary_status if result else None,
            handoff_status=result.handoff_status if result else None,
            messages=tuple(messages),
        )


__all__ = [
    "AnalyticsView",
    "ConversationCreated",
    "ConversationView",
    "CoordinatorError",
    "ScreeningView",
    "TurnCoordinator",
    "TurnCoordinatorResult",
    "generate_resume_token",
    "hash_resume_token",
]
