from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from candidate_screening.application.analytics import AnalyticsEvent, aggregate_events
from candidate_screening.application.reengagement import ReengagementService
from candidate_screening.domain.enums import ConversationStatus, ScreeningStatus
from candidate_screening.evals import load_eval_cases, load_scenarios, run_deterministic
from candidate_screening.persistence import Base, SqlAlchemyUnitOfWork
from candidate_screening.persistence.database import (
    create_async_engine_for_url,
    create_session_factory,
)
from candidate_screening.persistence.orm import CandidateORM, ConversationORM, ScreeningSessionORM


def test_analytics_aggregate_is_stable_for_unknown_events() -> None:
    aggregate = aggregate_events(
        [
            AnalyticsEvent("turn_completed"),
            {"event_type": "turn_failed", "event_metadata": {"error_code": "x"}},
            AnalyticsEvent("future_event"),
        ]
    )
    assert aggregate.total_events == 3
    assert aggregate.turns_completed == 1
    assert aggregate.turns_failed == 1
    assert aggregate.event_counts["future_event"] == 1


@pytest.mark.asyncio
async def test_reengagement_dry_run_apply_and_terminal_suppression(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    engine = create_async_engine_for_url(f"sqlite:///{tmp_path / 'reengage.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.candidates is not None and uow.sessions is not None
        assert uow.conversations is not None
        candidate = CandidateORM(full_name="Ana")
        await uow.candidates.add(candidate)
        session = ScreeningSessionORM(
            candidate_id=candidate.id,
            status=ScreeningStatus.IN_PROGRESS.value,
            last_activity_at=now - timedelta(days=2),
        )
        await uow.sessions.add(session)
        conversation = ConversationORM(
            screening_session_id=session.id,
            status=ConversationStatus.ACTIVE.value,
            resume_token_hash="hash-1",
        )
        await uow.conversations.add(conversation)
        await uow.commit()

    service = ReengagementService(
        lambda: SqlAlchemyUnitOfWork(factory), inactivity_hours=1, clock=lambda: now
    )
    preview = await service.run(dry_run=True, now=now)
    assert preview.sent == 0
    assert preview.eligible == 1
    applied = await service.run(dry_run=False, now=now)
    assert applied.sent == 1
    retried = await service.run(dry_run=False, now=now)
    assert retried.sent == 0
    assert retried.suppressed == 1

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.conversations is not None
        saved = await uow.conversations.get(conversation.id)
        assert saved is not None
        assert saved.reengagement_count == 1
        assert saved.last_reengagement_at is not None
        assert saved.last_reengagement_at.replace(tzinfo=UTC) == now
    await engine.dispose()


def test_deterministic_eval_fixtures_are_network_free() -> None:
    report = run_deterministic(load_eval_cases(), load_scenarios())
    assert report.total >= 8
    assert report.failed == 0
