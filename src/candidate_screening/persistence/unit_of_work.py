"""Transactional repository bundle."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .repositories import (
    AuditEventRepository,
    CandidateRepository,
    ConversationRepository,
    MessageRepository,
    RecruiterReviewRepository,
    ScreeningResultRepository,
    ScreeningSessionRepository,
    TurnRepository,
)


class SqlAlchemyUnitOfWork:
    """Keep database transactions short and explicit around application work."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.session: AsyncSession | None = None
        self.candidates: CandidateRepository | None = None
        self.sessions: ScreeningSessionRepository | None = None
        self.conversations: ConversationRepository | None = None
        self.turns: TurnRepository | None = None
        self.messages: MessageRepository | None = None
        self.results: ScreeningResultRepository | None = None
        self.audit_events: AuditEventRepository | None = None
        self.reviews: RecruiterReviewRepository | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self.session = self.session_factory()
        self.candidates = CandidateRepository(self.session)
        self.sessions = ScreeningSessionRepository(self.session)
        self.conversations = ConversationRepository(self.session)
        self.turns = TurnRepository(self.session)
        self.messages = MessageRepository(self.session)
        self.results = ScreeningResultRepository(self.session)
        self.audit_events = AuditEventRepository(self.session)
        self.reviews = RecruiterReviewRepository(self.session)
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        assert self.session is not None
        if exc_type is not None:
            await self.session.rollback()
        await self.session.close()

    async def commit(self) -> None:
        assert self.session is not None
        await self.session.commit()

    async def rollback(self) -> None:
        assert self.session is not None
        await self.session.rollback()
