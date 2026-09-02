"""Small SQLAlchemy repositories.

Repositories intentionally expose persistence models rather than attempting to
mirror every ORM object with a ceremonial DTO. Domain conversion belongs at
the application boundary, while these classes own query details and indexes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from candidate_screening.domain.enums import ScreeningStatus
from candidate_screening.domain.transitions import assert_transition

from .orm import (
    AuditEventORM,
    CandidateORM,
    ConversationORM,
    MessageORM,
    RecruiterReviewORM,
    ScreeningResultORM,
    ScreeningSessionORM,
    TurnORM,
)


class CandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, candidate_id: str) -> CandidateORM | None:
        return await self.session.get(CandidateORM, candidate_id)

    async def add(self, candidate: CandidateORM) -> CandidateORM:
        self.session.add(candidate)
        await self.session.flush()
        return candidate


class ScreeningSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, session_id: str) -> ScreeningSessionORM | None:
        return await self.session.get(ScreeningSessionORM, session_id)

    async def add(self, screening_session: ScreeningSessionORM) -> ScreeningSessionORM:
        self.session.add(screening_session)
        await self.session.flush()
        return screening_session

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ScreeningSessionORM]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("invalid screening-session pagination")
        statement = select(ScreeningSessionORM).order_by(desc(ScreeningSessionORM.last_activity_at))
        if status is not None:
            statement = statement.where(ScreeningSessionORM.status == status)
        result = await self.session.scalars(statement.offset(offset).limit(limit))
        return result.all()

    async def save_state(
        self,
        session_id: str,
        *,
        state: dict[str, Any],
        status: str,
        expected_version: int,
        current_field: str | None = None,
        clarification_counts: dict[str, Any] | None = None,
        last_activity_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ScreeningSessionORM | None:
        """Atomically save state if no concurrent turn changed its version.

        Returning ``None`` is an explicit optimistic-concurrency conflict; the
        caller can reject the turn and ask the client to retry from fresh
        state instead of merging two uncertain model updates.
        """

        current = await self.get(session_id)
        if current is None or current.version != expected_version:
            return None
        try:
            current_status = ScreeningStatus(current.status)
            target_status = ScreeningStatus(status)
        except ValueError as exc:
            raise ValueError("invalid screening status") from exc
        assert_transition(current_status, target_status)

        values: dict[str, Any] = {
            "screening_state": state,
            "status": status,
            "current_field": current_field,
            "version": expected_version + 1,
        }
        if clarification_counts is not None:
            values["clarification_counts"] = clarification_counts
        if last_activity_at is not None:
            values["last_activity_at"] = last_activity_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        statement = (
            update(ScreeningSessionORM)
            .where(
                ScreeningSessionORM.id == session_id,
                ScreeningSessionORM.version == expected_version,
            )
            .values(**values)
        )
        result = cast(CursorResult[Any], await self.session.execute(statement))
        if result.rowcount != 1:
            return None
        await self.session.flush()
        return await self.get(session_id)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, conversation_id: str) -> ConversationORM | None:
        return await self.session.get(ConversationORM, conversation_id)

    async def get_by_resume_hash(self, token_hash: str) -> ConversationORM | None:
        statement = select(ConversationORM).where(ConversationORM.resume_token_hash == token_hash)
        return await self.session.scalar(statement)

    async def get_by_session(self, session_id: str) -> ConversationORM | None:
        statement = select(ConversationORM).where(
            ConversationORM.screening_session_id == session_id
        )
        return await self.session.scalar(statement)

    async def add(self, conversation: ConversationORM) -> ConversationORM:
        self.session.add(conversation)
        await self.session.flush()
        return conversation


class TurnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, turn_id: str) -> TurnORM | None:
        return await self.session.get(TurnORM, turn_id)

    async def get_by_idempotency(self, conversation_id: str, key: str) -> TurnORM | None:
        statement = select(TurnORM).where(
            TurnORM.conversation_id == conversation_id,
            TurnORM.idempotency_key == key,
        )
        return await self.session.scalar(statement)

    async def count_for_conversation(self, conversation_id: str) -> int:
        statement = select(func.count(TurnORM.id)).where(TurnORM.conversation_id == conversation_id)
        return int(await self.session.scalar(statement) or 0)

    async def latest_for_conversation(self, conversation_id: str) -> TurnORM | None:
        """Return the most recently created turn for a conversation.

        Summary recovery needs a stable audit parent, but the result row only
        stores the screening session.  Keeping this query in the repository
        avoids leaking ordering details into the coordinator.
        """

        statement = (
            select(TurnORM)
            .where(TurnORM.conversation_id == conversation_id)
            .order_by(TurnORM.created_at.desc(), TurnORM.id.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)

    async def add(self, turn: TurnORM) -> TurnORM:
        self.session.add(turn)
        await self.session.flush()
        return turn

    async def save_processing_failure(
        self,
        turn_id: str,
        *,
        error_code: str,
        response_message: str,
        latency_ms: int | None = None,
    ) -> TurnORM | None:
        values: dict[str, Any] = {
            "status": "failed",
            "error_code": error_code[:100],
            "response_message": response_message[:2_000],
            "completed_at": datetime.now(UTC),
        }
        if latency_ms is not None:
            values["latency_ms"] = max(0, latency_ms)
        await self.session.execute(update(TurnORM).where(TurnORM.id == turn_id).values(**values))
        await self.session.flush()
        return await self.get(turn_id)


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, message: MessageORM) -> MessageORM:
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_for_conversation(
        self, conversation_id: str, *, limit: int = 200
    ) -> Sequence[MessageORM]:
        if limit < 1:
            raise ValueError("message limit must be positive")
        statement = (
            select(MessageORM)
            .where(MessageORM.conversation_id == conversation_id)
            .order_by(MessageORM.created_at.desc())
            .limit(limit)
        )
        result = await self.session.scalars(statement)
        # Consumers build chronological model history, while the query keeps
        # the bound focused on the newest messages.
        return tuple(reversed(result.all()))


class ScreeningResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_session(self, session_id: str) -> ScreeningResultORM | None:
        statement = select(ScreeningResultORM).where(
            ScreeningResultORM.screening_session_id == session_id
        )
        return await self.session.scalar(statement)

    async def add(self, result: ScreeningResultORM) -> ScreeningResultORM:
        self.session.add(result)
        await self.session.flush()
        return result


class AuditEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, event: AuditEventORM) -> AuditEventORM:
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_for_session(
        self, session_id: str, *, limit: int = 200
    ) -> Sequence[AuditEventORM]:
        if limit < 1:
            raise ValueError("audit-event limit must be positive")
        statement = (
            select(AuditEventORM)
            .where(AuditEventORM.screening_session_id == session_id)
            .order_by(AuditEventORM.created_at.asc())
            .limit(limit)
        )
        result = await self.session.scalars(statement)
        return result.all()

    async def list(
        self,
        *,
        event_type: str | None = None,
        limit: int = 10_000,
        offset: int = 0,
    ) -> Sequence[AuditEventORM]:
        """List audit rows for offline analytics jobs.

        The explicit bound prevents a dashboard request from accidentally
        materialising an unbounded event stream.  Events are ordered oldest
        first so replay and aggregate tests are deterministic.
        """

        if not 1 <= limit <= 100_000 or offset < 0:
            raise ValueError("invalid audit-event pagination")
        statement = select(AuditEventORM).order_by(
            AuditEventORM.created_at.asc(), AuditEventORM.id.asc()
        )
        if event_type is not None:
            statement = statement.where(AuditEventORM.event_type == event_type)
        result = await self.session.scalars(statement.offset(offset).limit(limit))
        return result.all()

    list_all = list


class RecruiterReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, review: RecruiterReviewORM) -> RecruiterReviewORM:
        self.session.add(review)
        await self.session.flush()
        return review
