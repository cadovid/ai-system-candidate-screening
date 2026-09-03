from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candidate_screening.ai.interpreter import InterpreterResult
from candidate_screening.ai.schemas import ExtractedLocation, ExtractedValue, TurnInterpretation
from candidate_screening.application.conversation import ConversationController
from candidate_screening.application.coordinator import CoordinatorError, TurnCoordinator
from candidate_screening.domain.enums import Language, ScreeningStatus
from candidate_screening.domain.rules import ScreeningEngine
from candidate_screening.domain.service_areas import ServiceAreaMatcher
from candidate_screening.persistence import Base, SqlAlchemyUnitOfWork
from candidate_screening.persistence.database import (
    create_async_engine_for_url,
    create_session_factory,
)


class CoordinatorBuilder(Protocol):
    def __call__(
        self, interpreter: QueueInterpreter, *, max_turns: int = 40
    ) -> TurnCoordinator: ...


class QueueInterpreter:
    def __init__(self, interpretations: list[TurnInterpretation]) -> None:
        self.interpretations = interpretations
        self.calls = 0

    async def interpret(
        self, message: str, dependencies: Any, *, message_history: Any = ()
    ) -> InterpreterResult:
        _ = (message, dependencies, message_history)
        self.calls += 1
        index = min(self.calls - 1, len(self.interpretations) - 1)
        return InterpreterResult(interpretation=self.interpretations[index])


@pytest.fixture
async def coordinator_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder]]:
    engine = create_async_engine_for_url(f"sqlite:///{tmp_path / 'workflow.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    matcher = ServiceAreaMatcher.from_file(
        Path(__file__).parents[1] / "data" / "service_areas" / "service_areas.json"
    )

    def build(interpreter: QueueInterpreter, *, max_turns: int = 40) -> TurnCoordinator:
        controller = ConversationController(ScreeningEngine(), matcher)
        return TurnCoordinator(
            lambda: SqlAlchemyUnitOfWork(factory),
            controller,
            interpreter,
            max_turns=max_turns,
        )

    yield factory, build
    await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_location_continues_then_replays_terminal_handoff(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ana", provided=True)),
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "Ana", "name")
    clarification = await coordinator.process_turn(created.conversation_id, "Madrid", "area-1")
    assert clarification.screening_status is ScreeningStatus.IN_PROGRESS
    assert clarification.next_field == "location"

    retry = await coordinator.process_turn(created.conversation_id, "Madrid", "area-2")
    assert retry.screening_status is ScreeningStatus.IN_PROGRESS

    handoff = await coordinator.process_turn(created.conversation_id, "Madrid", "area-3")
    assert handoff.screening_status is ScreeningStatus.NEEDS_REVIEW
    assert handoff.decision is not None
    assert handoff.decision.reason_codes == ["retry_limit"]
    assert interpreter.calls == 4

    replay = await coordinator.process_turn(created.conversation_id, "Madrid", "area-3")
    assert replay.idempotent is True
    assert replay.screening_status is ScreeningStatus.NEEDS_REVIEW
    assert interpreter.calls == 4

    with pytest.raises(CoordinatorError, match="closed"):
        await coordinator.process_turn(created.conversation_id, "another", "area-4")


@pytest.mark.asyncio
async def test_exact_location_clarification_resolves_ambiguous_match(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
            TurnInterpretation(
                location=ExtractedLocation(raw_value="Madrid Centro", provided=True)
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    ambiguous = await coordinator.process_turn(created.conversation_id, "Madrid", "ambiguous")
    assert ambiguous.screening_status is ScreeningStatus.IN_PROGRESS
    resolved = await coordinator.process_turn(created.conversation_id, "Madrid Centro", "resolved")
    assert resolved.screening_status is ScreeningStatus.IN_PROGRESS
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.confirmed is True
    assert view.state.location.service_area_id == "es-mad-centro"


@pytest.mark.asyncio
async def test_single_fuzzy_suggestion_stays_pending_until_confirmation(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrd centro", provided=True)),
            TurnInterpretation(confirmation=True),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    suggestion = await coordinator.process_turn(created.conversation_id, "Madrd centro", "fuzzy")
    assert suggestion.screening_status is ScreeningStatus.IN_PROGRESS
    assert suggestion.next_field == "location"
    assert "Do you mean" in suggestion.assistant_message

    confirmed = await coordinator.process_turn(created.conversation_id, "yes", "confirm")
    assert confirmed.screening_status is ScreeningStatus.IN_PROGRESS
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.confirmed is True
    assert view.state.location.service_area_id == "es-mad-centro"


@pytest.mark.asyncio
async def test_known_city_confirmation_is_persisted_as_explicit_area_scope(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
            ),
            # The coordinator recovers an exact yes/no answer when a provider
            # returns a valid interpretation but omits the confirmation flag.
            TurnInterpretation(),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    offer = await coordinator.process_turn(created.conversation_id, "Madrid", "city")
    assert offer.screening_status is ScreeningStatus.IN_PROGRESS
    assert "Centro" in offer.assistant_message
    assert "Salamanca" in offer.assistant_message

    accepted = await coordinator.process_turn(created.conversation_id, "yes", "city-yes")
    assert accepted.screening_status is ScreeningStatus.IN_PROGRESS
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.location.match_status.value == "exact"
    assert view.state.location.confirmed is True
    assert view.state.location.service_area_ids == [
        "es-mad-centro",
        "es-mad-salamanca",
    ]
    assert view.state.pending_confirmation is None


@pytest.mark.asyncio
async def test_max_turns_handoff_does_not_call_provider_after_bound(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [TurnInterpretation(), TurnInterpretation(), TurnInterpretation()]
    )
    coordinator = build(interpreter, max_turns=2)
    created = await coordinator.create_conversation(language=Language.EN)

    first = await coordinator.process_turn(created.conversation_id, "one", "one")
    assert first.screening_status is ScreeningStatus.IN_PROGRESS
    second = await coordinator.process_turn(created.conversation_id, "two", "two")
    assert second.screening_status is ScreeningStatus.NEEDS_REVIEW
    assert second.decision is not None
    assert second.decision.reason_codes == ["max_turns_exceeded"]
    assert interpreter.calls == 2

    replay = await coordinator.process_turn(created.conversation_id, "two", "two")
    assert replay.idempotent is True
    assert interpreter.calls == 2


@pytest.mark.asyncio
async def test_commit_synchronizes_name_and_disclosure_acknowledgement(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                disclosure_acknowledged=True,
                full_name=ExtractedValue(value=" Ana García ", provided=True),
            )
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)
    await coordinator.process_turn(created.conversation_id, "Ana García", "name")

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.sessions and uow.candidates and uow.conversations
        session = await uow.sessions.get(created.session_id)
        assert session is not None
        candidate = await uow.candidates.get(session.candidate_id)
        conversation = await uow.conversations.get(created.conversation_id)
        assert candidate is not None and candidate.full_name == "Ana García"
        assert conversation is not None and conversation.disclosure_acknowledged_at is not None
