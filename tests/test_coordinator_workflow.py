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
from candidate_screening.application.faq import FAQCatalog
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
    faq = FAQCatalog.from_file(Path(__file__).parents[1] / "data" / "faq" / "faq.json")

    def build(interpreter: QueueInterpreter, *, max_turns: int = 40) -> TurnCoordinator:
        controller = ConversationController(ScreeningEngine(), matcher, faq_catalog=faq)
        return TurnCoordinator(
            lambda: SqlAlchemyUnitOfWork(factory),
            controller,
            interpreter,
            max_turns=max_turns,
        )

    yield factory, build
    await engine.dispose()


@pytest.mark.asyncio
async def test_simple_ack_and_name_answers_advance_without_an_empty_model_patch(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A short name answer must not loop when a provider returns ``{}``."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    acknowledged = await coordinator.process_turn(
        created.conversation_id, "Empecemos por favor", "ack"
    )
    assert acknowledged.next_field == "full_name"
    assert "nombre completo" in acknowledged.assistant_message

    answered = await coordinator.process_turn(created.conversation_id, "Maria Pineda", "name")
    assert answered.screening_status is ScreeningStatus.IN_PROGRESS
    assert answered.next_field == "drivers_license"
    assert "licencia" in answered.assistant_message
    # A properly formatted bare name is a deterministic fast path; no model
    # call is needed for this unambiguous value.
    assert interpreter.calls == 0

    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.disclosure_acknowledged is True
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Maria Pineda"


@pytest.mark.asyncio
async def test_name_answer_repairs_legacy_missing_disclosure_state(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A name answer still advances sessions created before disclosure was persisted."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    # This models the inconsistent persisted state observed in the reported
    # bug: the controller had already asked for the name, but the disclosure
    # acknowledgement flag was still false.
    answered = await coordinator.process_turn(
        created.conversation_id, "Maria Pineda", "legacy-name"
    )

    assert answered.next_field == "drivers_license"
    assert answered.screening_status is ScreeningStatus.IN_PROGRESS
    assert interpreter.calls == 0
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.disclosure_acknowledged is True
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Maria Pineda"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "affirmative"),
    [(Language.ES, "sí"), (Language.EN, "yes")],
)
async def test_affirmative_after_direct_name_answers_active_license_question(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    language: Language,
    affirmative: str,
) -> None:
    """A yes/no reply after a direct name answer must not be consumed as disclosure."""

    _, build = coordinator_factory
    # The provider recognizes the name but does not emit a disclosure flag,
    # which is the persisted state that exposed the production loop.
    interpreter = QueueInterpreter(
        [TurnInterpretation(full_name=ExtractedValue(value="Maria Pineda", provided=True))]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=language)

    # Lowercase formatting deliberately keeps this test on the model path so
    # it continues to model a provider response that omits disclosure.
    name_result = await coordinator.process_turn(created.conversation_id, "maria pineda", "name")
    assert name_result.next_field == "drivers_license"

    license_result = await coordinator.process_turn(created.conversation_id, affirmative, "license")
    assert license_result.screening_status is ScreeningStatus.IN_PROGRESS
    assert license_result.next_field == "location"
    location_prompt = license_result.assistant_message.casefold()
    assert ("city" in location_prompt) or ("ciudad" in location_prompt)
    assert interpreter.calls == 1

    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.drivers_license is not None
    assert view.state.drivers_license.value is True


@pytest.mark.asyncio
async def test_labelled_multi_field_answer_uses_the_shared_deterministic_path(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A labelled first reply should fill all fields without an LLM call."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    first = await coordinator.process_turn(
        created.conversation_id,
        (
            "Name: Isla Morgan; licence: yes; location: Valencia centro; "
            "availability: full time; schedule: afternoon; "
            "experience: 5 years Deliveroo; start: next month"
        ),
        "multi-field",
    )
    assert first.screening_status is ScreeningStatus.IN_PROGRESS
    assert first.next_field is None
    assert "everything correct" in first.assistant_message
    assert interpreter.calls == 0

    confirmed = await coordinator.process_turn(created.conversation_id, "yes", "final-yes")
    assert confirmed.screening_status is ScreeningStatus.QUALIFIED
    assert confirmed.decision is not None
    assert confirmed.decision.reason_codes == ["all_explicit_criteria_met"]
    assert interpreter.calls == 0


@pytest.mark.asyncio
async def test_explicit_language_switch_is_handled_without_provider_call(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    named = await coordinator.process_turn(created.conversation_id, "Me llamo Lucía Pérez", "name")
    assert named.next_field == "drivers_license"

    switched = await coordinator.process_turn(
        created.conversation_id, "Please continue in English", "switch"
    )
    assert switched.screening_status is ScreeningStatus.IN_PROGRESS
    assert switched.next_field == "drivers_license"
    assert "valid driver's licence" in switched.assistant_message
    assert interpreter.calls == 0

    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.preferred_language is Language.EN


@pytest.mark.asyncio
async def test_question_is_answered_or_bounded_before_screening_resumes(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    faq = await coordinator.process_turn(
        created.conversation_id, "What schedules are available?", "faq"
    )
    assert faq.screening_status is ScreeningStatus.IN_PROGRESS
    assert faq.next_field == "full_name"
    assert "Morning" in faq.assistant_message
    assert "full name" in faq.assistant_message

    unknown = await coordinator.process_turn(
        created.conversation_id, "What is the salary?", "unknown"
    )
    assert unknown.screening_status is ScreeningStatus.IN_PROGRESS
    assert unknown.next_field == "full_name"
    assert "don’t have that information" in unknown.assistant_message
    assert interpreter.calls == 0


@pytest.mark.asyncio
async def test_off_topic_message_is_bounded_without_a_provider_call(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(created.conversation_id, "Tell me a joke", "off-topic")
    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == "full_name"
    assert "don’t have that information" in result.assistant_message
    assert interpreter.calls == 0


@pytest.mark.asyncio
async def test_known_location_mislabelled_as_name_is_reconciled_as_location(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ana Ruiz", provided=True)),
            TurnInterpretation(full_name=ExtractedValue(value="Madrid centro", provided=True)),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "ana ruiz", "name")
    await coordinator.process_turn(created.conversation_id, "yes", "license")
    location = await coordinator.process_turn(
        created.conversation_id, "The city center of Madrid", "location"
    )

    assert location.screening_status is ScreeningStatus.IN_PROGRESS
    assert location.next_field == "availability"
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Ana Ruiz"
    assert view.state.location.service_area_id == "es-mad-centro"
    assert view.state.location.confirmed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_area"),
    [
        ("Madrid centro", "es-mad-centro"),
        ("The city center of Madrid", "es-mad-centro"),
    ],
)
async def test_exact_location_alias_bypasses_provider_and_is_reconciled(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    message: str,
    expected_area: str,
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            # The exact aliases are handled before this response is consumed;
            # retaining a provider result here also documents that the queue
            # must not be advanced by the location turn.
            TurnInterpretation(
                location=(
                    ExtractedLocation(
                        raw_value="Madrid centro",
                        city="Madrid",
                        zone="Centro",
                        provided=False,
                        evidence=message,
                    )
                    if message == "Madrid centro"
                    else None
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "ada lovelace", "name")
    await coordinator.process_turn(created.conversation_id, "I have a valid licence", "licence")
    location = await coordinator.process_turn(created.conversation_id, message, "location")

    assert location.screening_status is ScreeningStatus.IN_PROGRESS
    assert location.next_field == "availability"
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.location.service_area_id == expected_area
    assert view.state.location.match_status.value == "exact"
    assert view.state.location.confirmed is True
    assert interpreter.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "city_answer", "expected_prompt_fragment"),
    [
        (Language.EN, "Madrid", "Can you deliver"),
        (Language.ES, "Madrid", "¿Puedes repartir"),
    ],
)
async def test_known_city_location_creates_safe_pending_city_offer_without_provider(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    language: Language,
    city_answer: str,
    expected_prompt_fragment: str,
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=language)

    await coordinator.process_turn(created.conversation_id, "ada lovelace", "name")
    await coordinator.process_turn(created.conversation_id, "I have a valid licence", "licence")
    offer = await coordinator.process_turn(created.conversation_id, city_answer, "location")

    assert offer.screening_status is ScreeningStatus.IN_PROGRESS
    assert offer.next_field == "location"
    assert expected_prompt_fragment in offer.assistant_message
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.location.city == "Madrid"
    assert view.state.location.service_area_id is None
    assert view.state.location.service_area_ids == []
    assert view.state.location.confirmed is False
    assert view.state.pending_confirmation is not None
    assert view.state.pending_confirmation.reason == "service_area_city"
    assert interpreter.calls == 2


@pytest.mark.asyncio
async def test_contradictory_model_location_cannot_override_raw_candidate_message(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(
                location=ExtractedLocation(
                    raw_value="Madrid centro",
                    city="Madrid",
                    zone="Centro",
                    # No evidence means the model patch must not be trusted
                    # when the candidate's raw answer names another city.
                    provided=False,
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "ada lovelace", "name")
    await coordinator.process_turn(created.conversation_id, "I have a valid licence", "licence")
    location = await coordinator.process_turn(created.conversation_id, "Bilbao", "location")

    assert location.screening_status is ScreeningStatus.IN_PROGRESS
    assert location.next_field == "location"
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.location.service_area_id is None
    assert view.state.location.match_status.value == "unresolved"
    assert view.state.pending_confirmation is None
    # Unsupported text remains model-owned; the location shortcut must not
    # hide the provider call or turn it into a catalogue area.
    assert interpreter.calls == 3


@pytest.mark.asyncio
async def test_pending_city_zone_answer_is_resolved_without_provider(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
            # A trusted pending-city zone answer is resolved before the
            # provider is consulted.
            TurnInterpretation(),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "ada lovelace", "name")
    await coordinator.process_turn(created.conversation_id, "I have a valid licence", "licence")
    offer = await coordinator.process_turn(created.conversation_id, "Madrid", "city")
    assert offer.next_field == "location"

    selected = await coordinator.process_turn(created.conversation_id, "Centro", "zone")

    assert selected.screening_status is ScreeningStatus.IN_PROGRESS
    assert selected.next_field == "availability"
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.service_area_id == "es-mad-centro"
    assert view.state.location.confirmed is True
    assert interpreter.calls == 2


@pytest.mark.asyncio
async def test_explicit_name_correction_is_confirmed_without_provider_call(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(
        created.conversation_id,
        (
            "Name: Riley Stone; licence: yes; location: Madrid centro; "
            "availability: full time; schedule: morning; experience: 2 years; start: ASAP"
        ),
        "initial",
    )
    correction = await coordinator.process_turn(
        created.conversation_id,
        "Actually, my full name is Riley Jones",
        "correction",
    )
    assert correction.screening_status is ScreeningStatus.IN_PROGRESS
    assert correction.next_field == "full_name"
    assert "replace the previous information" in correction.assistant_message
    assert interpreter.calls == 0

    accepted = await coordinator.process_turn(created.conversation_id, "yes", "accept")
    assert accepted.screening_status is ScreeningStatus.IN_PROGRESS
    assert accepted.next_field is None
    final = await coordinator.process_turn(created.conversation_id, "yes", "final")
    assert final.screening_status is ScreeningStatus.QUALIFIED
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Riley Jones"


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
