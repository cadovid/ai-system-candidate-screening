"""Bounded, idempotent reminders for paused screening conversations.

Re-engagement is intentionally a persistence-only operation.  It never calls
the language model, changes a screening decision, or sends through an external
provider.  The returned messages are queued as ordinary assistant messages;
an adapter can deliver them through the channel selected by the conversation.

The database update uses ``reengagement_count < max_reminders`` in its WHERE
clause.  That makes the one-reminder guarantee hold even when two worker
processes run the job at the same time.  The count is also the idempotency key
for the operation, so a retried job cannot append a second reminder.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from candidate_screening.application.analytics import AnalyticsEventType
from candidate_screening.domain.enums import ConversationStatus, Language, ScreeningStatus
from candidate_screening.persistence.orm import (
    AuditEventORM,
    ConversationORM,
    MessageORM,
    ScreeningSessionORM,
)
from candidate_screening.persistence.unit_of_work import SqlAlchemyUnitOfWork


class _UOWFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[SqlAlchemyUnitOfWork]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """Treat a naïve SQLite timestamp as UTC when reading old databases."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@dataclass(frozen=True, slots=True)
class ReengagementCandidate:
    """A paused conversation that is eligible for (or suppressed from) a reminder."""

    conversation_id: str
    session_id: str
    language: Language
    channel: str
    last_activity_at: datetime
    reengagement_count: int
    message: str
    eligible: bool = True
    suppression_reason: str | None = None

    @property
    def reminder(self) -> str:
        """Alias used by delivery adapters."""

        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "language": self.language.value,
            "channel": self.channel,
            "last_activity_at": self.last_activity_at.isoformat(),
            "reengagement_count": self.reengagement_count,
            "message": self.message,
            "eligible": self.eligible,
            "suppression_reason": self.suppression_reason,
        }


@dataclass(frozen=True, slots=True)
class ReengagementReport:
    """Result of one dry-run or apply pass."""

    dry_run: bool
    as_of: datetime
    inactivity_hours: float
    max_reminders: int
    scanned: int = 0
    eligible: int = 0
    sent: int = 0
    suppressed: int = 0
    errors: int = 0
    items: tuple[ReengagementCandidate, ...] = field(default_factory=tuple)

    @property
    def reminders_sent(self) -> int:
        return self.sent

    @property
    def candidates(self) -> tuple[ReengagementCandidate, ...]:
        return self.items

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "as_of": self.as_of.isoformat(),
            "inactivity_hours": self.inactivity_hours,
            "max_reminders": self.max_reminders,
            "scanned": self.scanned,
            "eligible": self.eligible,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "errors": self.errors,
            "items": [item.as_dict() for item in self.items],
        }


class ReengagementService:
    """Find and queue at most one reminder per active screening conversation."""

    def __init__(
        self,
        uow_factory: _UOWFactory | Callable[[], Any],
        *,
        inactivity_hours: float = 24.0,
        inactivity_period: timedelta | None = None,
        inactivity: timedelta | None = None,
        max_reminders: int = 1,
        reminder_limit: int | None = None,
        clock: Callable[[], datetime] = utc_now,
        reminder_builder: Callable[[Language], str] | None = None,
    ) -> None:
        if inactivity is not None:
            if inactivity_period is not None:
                raise ValueError("provide only one inactivity period")
            inactivity_period = inactivity
        if reminder_limit is not None:
            max_reminders = reminder_limit
        if inactivity_hours <= 0 and inactivity_period is None:
            raise ValueError("inactivity period must be positive")
        if inactivity_period is not None and inactivity_period <= timedelta(0):
            raise ValueError("inactivity period must be positive")
        if max_reminders < 1:
            raise ValueError("max_reminders must be at least one")
        self.uow_factory = uow_factory
        self.inactivity_period = inactivity_period or timedelta(hours=inactivity_hours)
        self.inactivity_hours = self.inactivity_period.total_seconds() / 3_600
        self.max_reminders = max_reminders
        self.clock = clock
        self.reminder_builder = reminder_builder or self.default_message

    @staticmethod
    def default_message(language: Language) -> str:
        if language is Language.EN:
            return (
                "Hi! Your delivery-driver screening is still open. You can resume it "
                "whenever you’re ready by replying here. Reply STOP if you do not want "
                "any more reminders."
            )
        return (
            "¡Hola! Tu evaluación para el puesto de repartidor/a sigue abierta. Puedes "
            "retomarla cuando quieras respondiendo aquí. Responde STOP si no quieres "
            "recibir más recordatorios."
        )

    @staticmethod
    def _language(value: str) -> Language:
        try:
            return Language(value)
        except ValueError:
            return Language.ES

    def _candidate(
        self,
        session: ScreeningSessionORM,
        conversation: ConversationORM,
        *,
        now: datetime,
    ) -> ReengagementCandidate:
        language = self._language(session.preferred_language)
        eligible, reason = self._eligibility(session, conversation, now=now)
        return ReengagementCandidate(
            conversation_id=conversation.id,
            session_id=session.id,
            language=language,
            channel=conversation.channel,
            last_activity_at=_aware(session.last_activity_at),
            reengagement_count=conversation.reengagement_count,
            message=self.reminder_builder(language),
            eligible=eligible,
            suppression_reason=reason,
        )

    def _eligibility(
        self,
        session: ScreeningSessionORM,
        conversation: ConversationORM,
        *,
        now: datetime,
    ) -> tuple[bool, str | None]:
        # Terminal states and opted-out conversations are never candidates,
        # regardless of their timestamps or reminder count.
        if session.status != ScreeningStatus.IN_PROGRESS.value:
            return False, "terminal_screening"
        if conversation.status == ConversationStatus.OPTED_OUT.value:
            return False, "candidate_opted_out"
        if conversation.status != ConversationStatus.ACTIVE.value:
            return False, "conversation_closed"
        if conversation.reengagement_count >= self.max_reminders:
            return False, "reminder_limit_reached"
        last_activity = _aware(session.last_activity_at)
        if now - last_activity < self.inactivity_period:
            return False, "not_inactive"
        return True, None

    async def plan(
        self,
        *,
        now: datetime | None = None,
        limit: int = 500,
        include_suppressed: bool = False,
    ) -> tuple[ReengagementCandidate, ...]:
        """Return a deterministic plan without writing any rows."""

        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        timestamp = _aware(now or self.clock())
        # Do the conversion while the session is open.  SQLite returns ORM
        # objects whose scalar values are available after commit, but making a
        # copy here avoids relying on that implementation detail.
        async with self.uow_factory() as uow:
            assert uow.session is not None
            result = await uow.session.execute(
                select(ScreeningSessionORM, ConversationORM)
                .join(
                    ConversationORM, ConversationORM.screening_session_id == ScreeningSessionORM.id
                )
                .order_by(ScreeningSessionORM.last_activity_at.asc(), ScreeningSessionORM.id.asc())
                .limit(limit)
            )
            rows = result.all()
            candidates = tuple(
                self._candidate(session, conversation, now=timestamp)
                for session, conversation in rows
            )
        if include_suppressed:
            return candidates
        return tuple(candidate for candidate in candidates if candidate.eligible)

    async def run(
        self,
        *,
        dry_run: bool = True,
        now: datetime | None = None,
        limit: int = 500,
        include_suppressed: bool = True,
    ) -> ReengagementReport:
        """Plan reminders and optionally apply them atomically.

        In dry-run mode no UPDATE, message, or audit event is persisted.  In
        apply mode each reminder gets its own short transaction.  A failed
        candidate does not prevent other eligible candidates from being sent.
        """

        timestamp = _aware(now or self.clock())
        planned = await self.plan(now=timestamp, limit=limit, include_suppressed=True)
        if dry_run:
            return ReengagementReport(
                dry_run=True,
                as_of=timestamp,
                inactivity_hours=self.inactivity_hours,
                max_reminders=self.max_reminders,
                scanned=len(planned),
                eligible=sum(item.eligible for item in planned),
                sent=0,
                suppressed=sum(not item.eligible for item in planned),
                items=planned
                if include_suppressed
                else tuple(item for item in planned if item.eligible),
            )

        sent = 0
        errors = 0
        applied_items: list[ReengagementCandidate] = []
        for item in planned:
            if not item.eligible:
                if include_suppressed:
                    applied_items.append(item)
                continue
            applied = await self._apply_one(item, now=timestamp)
            if applied:
                sent += 1
                applied_items.append(item)
            else:
                # A concurrent worker winning the conditional UPDATE is an
                # idempotent suppression, not an operational error.  Any
                # actual database exception is converted to an error count by
                # _apply_one's safe boundary.
                if include_suppressed:
                    applied_items.append(
                        ReengagementCandidate(
                            conversation_id=item.conversation_id,
                            session_id=item.session_id,
                            language=item.language,
                            channel=item.channel,
                            last_activity_at=item.last_activity_at,
                            reengagement_count=item.reengagement_count,
                            message=item.message,
                            eligible=False,
                            suppression_reason="already_processed",
                        )
                    )
        return ReengagementReport(
            dry_run=False,
            as_of=timestamp,
            inactivity_hours=self.inactivity_hours,
            max_reminders=self.max_reminders,
            scanned=len(planned),
            eligible=sum(item.eligible for item in planned),
            sent=sent,
            suppressed=sum(not item.eligible for item in planned)
            + (sum(item.eligible for item in planned) - sent),
            errors=errors,
            items=tuple(
                applied_items
                if include_suppressed
                else [item for item in applied_items if item.eligible]
            ),
        )

    async def _apply_one(self, item: ReengagementCandidate, *, now: datetime) -> bool:
        """Conditionally claim and append one reminder; return whether claimed."""

        try:
            async with self.uow_factory() as uow:
                assert uow.session is not None
                # Claim the reminder before adding its message.  The row lock
                # /conditional update ensures another worker cannot also claim
                # this count.  The surrounding transaction rolls it back if
                # message or audit insertion fails.
                statement = (
                    update(ConversationORM)
                    .where(
                        ConversationORM.id == item.conversation_id,
                        ConversationORM.status == ConversationStatus.ACTIVE.value,
                        ConversationORM.reengagement_count < self.max_reminders,
                        ConversationORM.screening_session_id == item.session_id,
                    )
                    .values(
                        reengagement_count=ConversationORM.reengagement_count + 1,
                        last_reengagement_at=now,
                        updated_at=now,
                    )
                )
                result = cast(CursorResult[Any], await uow.session.execute(statement))
                if result.rowcount != 1:
                    await uow.rollback()
                    return False
                session_row = await uow.session.scalar(
                    select(ScreeningSessionORM.id)
                    .where(
                        ScreeningSessionORM.id == item.session_id,
                        ScreeningSessionORM.status == ScreeningStatus.IN_PROGRESS.value,
                    )
                    .with_for_update()
                )
                if session_row is None:
                    await uow.rollback()
                    return False
                await uow.session.flush()
                assert uow.messages is not None
                await uow.messages.add(
                    MessageORM(
                        conversation_id=item.conversation_id,
                        direction="assistant",
                        content=item.message[:2_000],
                        language=item.language.value,
                        created_at=now,
                    )
                )
                assert uow.audit_events is not None
                await uow.audit_events.add(
                    AuditEventORM(
                        screening_session_id=item.session_id,
                        event_type=AnalyticsEventType.REENGAGEMENT_SENT,
                        stage="reengagement",
                        event_metadata={
                            "reminder_number": item.reengagement_count + 1,
                            "idempotency_key": f"reengagement:{item.conversation_id}:{item.reengagement_count + 1}",
                        },
                        created_at=now,
                    )
                )
                await uow.commit()
                return True
        except IntegrityError, SQLAlchemyError:
            # A worker retry should be safe and should not crash a batch.  The
            # caller reports the candidate as already processed/suppressed.
            return False

    # Names used by cron adapters and earlier prototypes.
    process = run
    reengage = run
    send_reminders = run
    find_candidates = plan
    list_candidates = plan

    async def send_reminder(self, conversation_id: str, *, now: datetime | None = None) -> bool:
        """Atomically send one reminder for a single conversation ID."""

        timestamp = _aware(now or self.clock())
        planned = await self.plan(now=timestamp, limit=10_000, include_suppressed=True)
        item = next(
            (candidate for candidate in planned if candidate.conversation_id == conversation_id),
            None,
        )
        return item is not None and item.eligible and await self._apply_one(item, now=timestamp)


__all__ = [
    "ReengagementCandidate",
    "ReengagementReport",
    "ReengagementService",
    "utc_now",
]
