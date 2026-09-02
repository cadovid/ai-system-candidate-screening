from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from candidate_screening.application.analytics import (
    AnalyticsEventType,
    aggregate_persisted_data,
)
from candidate_screening.application.coordinator import TurnCoordinator
from candidate_screening.domain.enums import ConversationStatus, ScreeningStatus, TurnStatus
from candidate_screening.persistence import Base, SqlAlchemyUnitOfWork
from candidate_screening.persistence.database import (
    create_async_engine_for_url,
    create_session_factory,
)
from candidate_screening.persistence.orm import (
    AuditEventORM,
    CandidateORM,
    ConversationORM,
    MessageORM,
    ScreeningResultORM,
    ScreeningSessionORM,
    TurnORM,
)


def test_persisted_analytics_aggregates_operational_rows_without_content() -> None:
    """Persisted metrics stay deterministic and contain no transcript data."""

    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    sessions = [
        {
            "id": "s-qualified",
            "status": "qualified",
            "preferred_language": "en",
            "started_at": started,
            "completed_at": started + timedelta(seconds=60),
            "clarification_counts": {"location": 2, "drivers_license": 1},
            "screening_state": {"candidate_confirmed": True},
        },
        {
            "id": "s-disqualified",
            "status": "disqualified",
            "preferred_language": "es",
            "started_at": started,
            "completed_at": started + timedelta(seconds=180),
            "clarification_counts": {"location": 1},
            "screening_state": {"candidate_confirmed": True},
        },
        {
            "id": "s-progress",
            "status": "in_progress",
            "preferred_language": "es",
            "started_at": started,
            "completed_at": None,
            "clarification_counts": {"availability": 0},
            "screening_state": {"current_field": "preferred_schedule"},
        },
        {
            "id": "s-abandoned",
            "status": "abandoned",
            "preferred_language": "en",
            "started_at": started,
            "completed_at": started + timedelta(seconds=30),
            "clarification_counts": {},
            "screening_state": {"current_field": "location"},
        },
    ]
    conversations = [
        {"id": "c-qualified", "screening_session_id": "s-qualified"},
        {"id": "c-disqualified", "screening_session_id": "s-disqualified"},
        {"id": "c-progress", "screening_session_id": "s-progress"},
        {"id": "c-abandoned", "screening_session_id": "s-abandoned"},
    ]
    turns = [
        {"id": "t-q1", "conversation_id": "c-qualified", "status": "completed"},
        {"id": "t-q2", "conversation_id": "c-qualified", "status": "completed"},
        {"id": "t-qf", "conversation_id": "c-qualified", "status": "failed"},
        {"id": "t-d1", "conversation_id": "c-disqualified", "status": "completed"},
    ]
    messages = [
        # The aggregate intentionally counts candidate messages from completed
        # turns only; introductions, assistant replies, and failed turns do
        # not change that metric.
        {"conversation_id": "c-qualified", "turn_id": "t-q1", "direction": "user"},
        {"conversation_id": "c-qualified", "turn_id": "t-q1", "direction": "assistant"},
        {"conversation_id": "c-qualified", "turn_id": "t-q2", "direction": "user"},
        {"conversation_id": "c-qualified", "turn_id": "t-q2", "direction": "assistant"},
        {"conversation_id": "c-qualified", "turn_id": "t-qf", "direction": "user"},
        {"conversation_id": "c-qualified", "turn_id": "t-qf", "direction": "assistant"},
        {"conversation_id": "c-disqualified", "turn_id": "t-d1", "direction": "user"},
    ]
    results = [
        {
            "screening_session_id": "s-disqualified",
            "status": "disqualified",
            "reason_codes": ["no_drivers_license"],
        }
    ]
    events: list[dict[str, Any]] = [
        {
            "event_type": "turn_completed",
            "turn_id": "t-q1",
            "event_metadata": {"faq_answered": True},
        },
        # Current producers write a dedicated FAQ event as well as the turn
        # metadata; one turn must count as one FAQ use.
        {
            "event_type": AnalyticsEventType.FAQ_ANSWERED,
            "turn_id": "t-q1",
            "event_metadata": {},
        },
        {
            "event_type": AnalyticsEventType.SCREENING_DISQUALIFIED,
            "screening_session_id": "s-disqualified",
            "event_metadata": {"reason_codes": ["outside_service_area"]},
        },
        # An audit-only export from an older producer still contributes when
        # no result row is available for that session.
        {
            "event_type": AnalyticsEventType.SCREENING_DISQUALIFIED,
            "screening_session_id": "s-audit-only",
            "event_metadata": {"reason_codes": ["outside_service_area"]},
        },
    ]

    aggregate = aggregate_persisted_data(sessions, conversations, turns, messages, results, events)

    assert aggregate.average_completed_messages == 1.5
    assert aggregate.average_completed_turns == 1.5
    assert aggregate.average_screening_duration_seconds == 120.0
    assert aggregate.language_distribution == {"en": 2, "es": 2}
    assert aggregate.faq_usage_count == 1
    assert aggregate.clarification_retry_count == 4
    assert aggregate.clarification_retry_counts == {"drivers_license": 1, "location": 3}
    assert aggregate.disqualification_reason_distribution == {
        "no_drivers_license": 1,
        "outside_service_area": 1,
    }
    assert aggregate.dropoff_stage_distribution == {
        "location": 1,
        "preferred_schedule": 1,
    }
    assert "candidate email" not in repr(asdict(aggregate))


def test_persisted_analytics_handles_empty_and_malformed_exports() -> None:
    aggregate = aggregate_persisted_data(
        sessions=[
            {
                "id": "s1",
                "status": "in_progress",
                "preferred_language": None,
                "screening_state": {},
                "clarification_counts": {"location": True, "availability": "bad"},
            }
        ],
        conversations=[],
        turns=[],
        messages=[],
        results=[],
        events=[],
    )

    assert aggregate.average_completed_messages is None
    assert aggregate.average_completed_turns is None
    assert aggregate.average_screening_duration_seconds is None
    assert aggregate.language_distribution == {"unknown": 1}
    assert aggregate.clarification_retry_count == 0
    assert aggregate.dropoff_stage_distribution == {}


@pytest.mark.asyncio
async def test_coordinator_analytics_reads_the_persisted_aggregate(tmp_path: Path) -> None:
    engine = create_async_engine_for_url(f"sqlite:///{tmp_path / 'analytics.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        candidate = CandidateORM(id="candidate-1", full_name="PII must not be returned")
        completed = ScreeningSessionORM(
            id="session-completed",
            candidate_id=candidate.id,
            status=ScreeningStatus.DISQUALIFIED.value,
            preferred_language="en",
            screening_state={"current_field": None},
            clarification_counts={"location": 1},
            started_at=started,
            last_activity_at=started + timedelta(seconds=90),
            completed_at=started + timedelta(seconds=90),
        )
        in_progress = ScreeningSessionORM(
            id="session-progress",
            candidate_id=candidate.id,
            status=ScreeningStatus.IN_PROGRESS.value,
            preferred_language="es",
            screening_state={"current_field": "location"},
            clarification_counts={},
            started_at=started,
            last_activity_at=started,
        )
        completed_conversation = ConversationORM(
            id="conversation-completed",
            screening_session_id=completed.id,
            status=ConversationStatus.COMPLETED.value,
            resume_token_hash="hash-completed",
        )
        progress_conversation = ConversationORM(
            id="conversation-progress",
            screening_session_id=in_progress.id,
            status=ConversationStatus.ACTIVE.value,
            resume_token_hash="hash-progress",
        )
        turn = TurnORM(
            id="turn-completed",
            conversation_id=completed_conversation.id,
            idempotency_key="key-1",
            status=TurnStatus.COMPLETED.value,
            created_at=started + timedelta(seconds=30),
        )
        result = ScreeningResultORM(
            id="result-1",
            screening_session_id=completed.id,
            status=ScreeningStatus.DISQUALIFIED.value,
            reason_codes=["no_drivers_license"],
            rule_trace={},
        )
        uow.session.add_all(
            [
                candidate,
                completed,
                in_progress,
                completed_conversation,
                progress_conversation,
                turn,
                MessageORM(
                    id="message-1",
                    conversation_id=completed_conversation.id,
                    turn_id=turn.id,
                    direction="user",
                    content="candidate email alice@example.test",
                    created_at=started + timedelta(seconds=31),
                ),
                result,
                AuditEventORM(
                    id="event-started",
                    screening_session_id=completed.id,
                    event_type=AnalyticsEventType.CONVERSATION_STARTED,
                    event_metadata={},
                    created_at=started,
                ),
                AuditEventORM(
                    id="event-faq",
                    screening_session_id=completed.id,
                    turn_id=turn.id,
                    event_type=AnalyticsEventType.FAQ_ANSWERED,
                    event_metadata={},
                    created_at=started + timedelta(seconds=32),
                ),
            ]
        )
        await uow.commit()

    coordinator = TurnCoordinator(
        lambda: SqlAlchemyUnitOfWork(factory), cast(Any, None), cast(Any, None)
    )
    analytics = await coordinator.get_analytics()

    assert analytics.average_completed_messages == 1.0
    assert analytics.average_completed_turns == 1.0
    assert analytics.average_screening_duration_seconds == 90.0
    assert analytics.language_distribution == {"en": 1, "es": 1}
    assert analytics.faq_usage_count == 1
    assert analytics.clarification_retry_count == 1
    assert analytics.disqualification_reason_distribution == {"no_drivers_license": 1}
    assert analytics.dropoff_stage_distribution == {"location": 1}
    assert "alice@example.test" not in repr(analytics)
    await engine.dispose()
