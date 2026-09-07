from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candidate_screening.ai.interpreter import InterpreterResult
from candidate_screening.ai.schemas import (
    ExtractedDeliveryExperience,
    ExtractedLocation,
    ExtractedValue,
    StartAvailabilityExtraction,
    TurnIntent,
    TurnInterpretation,
)
from candidate_screening.application.conversation import ConversationController
from candidate_screening.application.coordinator import CoordinatorError, TurnCoordinator
from candidate_screening.application.faq import FAQCatalog
from candidate_screening.domain.enums import (
    AvailabilityType,
    ConversationStatus,
    Language,
    SchedulePreference,
    ScreeningField,
    ScreeningStatus,
    TurnStatus,
)
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
    def __init__(
        self,
        interpretations: list[TurnInterpretation],
        *,
        usages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.interpretations = interpretations
        self.usages = usages or []
        self.calls = 0
        self.messages: list[str] = []

    async def interpret(
        self, message: str, dependencies: Any, *, message_history: Any = ()
    ) -> InterpreterResult:
        _ = (message, dependencies, message_history)
        self.calls += 1
        self.messages.append(message)
        index = min(self.calls - 1, len(self.interpretations) - 1)
        usage = self.usages[index] if index < len(self.usages) else {}
        return InterpreterResult(interpretation=self.interpretations[index], usage=usage)


def _complete_interpretation(name: str) -> TurnInterpretation:
    """Build a realistic semantic response containing every screening field."""

    return TurnInterpretation(
        detected_language=Language.EN,
        language_confidence=1.0,
        disclosure_acknowledged=True,
        full_name=ExtractedValue(value=name, provided=True, evidence=name),
        drivers_license=ExtractedValue(value=True, provided=True, evidence="valid licence"),
        location=ExtractedLocation(
            raw_value="Madrid centro",
            city="Madrid",
            zone="Centro",
            provided=True,
            evidence="Madrid centro",
        ),
        availability=ExtractedValue(
            value=[AvailabilityType.FULL_TIME],
            provided=True,
            evidence="full time",
        ),
        preferred_schedule=ExtractedValue(
            value=SchedulePreference.MORNING,
            provided=True,
            evidence="morning",
        ),
        delivery_experience=ExtractedDeliveryExperience(
            years=2,
            platforms=[],
            provided=True,
            evidence="2 years",
        ),
        start_availability=StartAvailabilityExtraction(
            raw_value="ASAP",
            precision="asap",
            provided=True,
            evidence="ASAP",
        ),
    )


async def _advance_to_availability(
    coordinator: TurnCoordinator,
    conversation_id: str,
) -> None:
    """Use ordinary candidate answers to reach availability through the model."""

    await coordinator.process_turn(conversation_id, "Ada Lovelace", "setup-name")
    await coordinator.process_turn(conversation_id, "yes", "setup-license")
    await coordinator.process_turn(conversation_id, "Madrid centro", "setup-location")


async def _turn_usage(
    factory: async_sessionmaker[AsyncSession],
    conversation_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.turns is not None
        turn = await uow.turns.get_by_idempotency(conversation_id, idempotency_key)
        assert turn is not None
        return dict(turn.model_usage or {})


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
async def test_normal_canonical_input_calls_model_first_when_output_is_sufficient(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A useful model extraction is applied without a deterministic detour."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                full_name=ExtractedValue(
                    value="Ada Lovelace",
                    provided=True,
                    evidence="Ada Lovelace",
                )
            )
        ],
        usages=[
            {
                "requests": 1,
                "input_tokens": 23,
                "output_tokens": 9,
                "provider_marker": "model-first",
            }
        ],
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(
        created.conversation_id,
        "Ada Lovelace",
        "model-first-name",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.DRIVERS_LICENSE.value
    assert interpreter.messages == ["Ada Lovelace"]
    assert interpreter.calls == 1
    usage = await _turn_usage(factory, created.conversation_id, "model-first-name")
    assert usage["provider_marker"] == "model-first"
    assert usage["llm_attempted"] is True
    assert usage["llm_succeeded"] is True
    assert usage["llm_sufficient"] is True
    assert usage["deterministic_fallback_attempted"] is False
    assert usage["deterministic_fallback_used"] is False
    assert usage["deterministic_fallback"] is False


@pytest.mark.asyncio
async def test_valid_empty_model_output_invokes_narrow_deterministic_fallback(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """An empty model patch may be completed only by the narrow fallback."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [TurnInterpretation()],
        usages=[
            {
                "requests": 1,
                "input_tokens": 31,
                "output_tokens": 6,
                "provider_marker": "preserve-provider-accounting",
            }
        ],
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(
        created.conversation_id,
        "Ada Lovelace",
        "empty-name-fallback",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.DRIVERS_LICENSE.value
    assert interpreter.messages == ["Ada Lovelace"]
    assert interpreter.calls == 1
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Ada Lovelace"
    usage = await _turn_usage(factory, created.conversation_id, "empty-name-fallback")
    # Provider accounting survives the deterministic completion, including
    # adapter metadata that the fallback does not know about.
    assert usage["requests"] == 1
    assert usage["input_tokens"] == 31
    assert usage["output_tokens"] == 6
    assert usage["provider_marker"] == "preserve-provider-accounting"
    assert usage["llm_attempted"] is True
    assert usage["llm_succeeded"] is True
    assert usage["llm_sufficient"] is False
    assert usage["deterministic_fallback_attempted"] is True
    assert usage["deterministic_fallback_used"] is True
    assert usage["deterministic_fallback"] is True
    assert usage["deterministic_fallback_trigger"] == "insufficient_llm_output"
    assert usage["deterministic_fallback_reason"] == "name_answer"


@pytest.mark.asyncio
async def test_ambiguous_model_location_is_not_overwritten_by_deterministic_recovery(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """An explicit model ambiguity remains unresolved for candidate clarification."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(
                location=ExtractedLocation(
                    raw_value="Madrid",
                    provided=True,
                    ambiguous=True,
                    evidence="Madrid could refer to more than one zone",
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "Ada Lovelace", "amb-name")
    await coordinator.process_turn(created.conversation_id, "yes", "amb-license")
    result = await coordinator.process_turn(created.conversation_id, "Madrid", "amb-location")

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.LOCATION.value
    assert "configured delivery areas" in result.assistant_message.casefold()
    assert interpreter.calls == 3
    usage = await _turn_usage(factory, created.conversation_id, "amb-location")
    assert usage["llm_sufficient"] is True
    assert usage["deterministic_fallback_attempted"] is False
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.location.match_status.value == "ambiguous"
    assert view.state.location.service_area_id is None
    assert view.state.location.suggestion_ids


@pytest.mark.asyncio
async def test_question_model_output_is_not_replaced_by_deterministic_field_guess(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A model question remains a conversational detour instead of a field answer."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                intent=TurnIntent.QUESTION,
                response_requested=True,
                candidate_questions=["What schedules are available?"],
            )
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(
        created.conversation_id,
        "What schedules are available?",
        "question-model",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.FULL_NAME.value
    assert "Morning" in result.assistant_message
    assert "full name" in result.assistant_message
    assert interpreter.calls == 1
    usage = await _turn_usage(factory, created.conversation_id, "question-model")
    assert usage["llm_sufficient"] is True
    assert usage["deterministic_fallback_attempted"] is False


@pytest.mark.asyncio
async def test_model_safety_output_is_not_replaced_by_deterministic_field_guess(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A model safety signal remains authoritative for the current turn."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                intent=TurnIntent.PROMPT_INJECTION,
                prompt_injection_detected=True,
            )
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(
        created.conversation_id,
        "Ada Lovelace",
        "safety-model",
    )

    assert result.status is TurnStatus.COMPLETED
    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert "internal instructions" in result.assistant_message
    assert interpreter.calls == 1
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.full_name is None
    usage = await _turn_usage(factory, created.conversation_id, "safety-model")
    assert usage["llm_sufficient"] is True
    assert usage["deterministic_fallback_attempted"] is False


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
    assert "aclar" not in acknowledged.assistant_message.casefold()

    answered = await coordinator.process_turn(created.conversation_id, "Laura Pineda", "name")
    assert answered.screening_status is ScreeningStatus.IN_PROGRESS
    assert answered.next_field == "drivers_license"
    assert "licencia" in answered.assistant_message
    # The model is attempted first; its empty responses are completed by the
    # narrow deterministic acknowledgement/name recovery.
    assert interpreter.calls == 2

    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.disclosure_acknowledged is True
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Laura Pineda"


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
        created.conversation_id, "Laura Pineda", "legacy-name"
    )

    assert answered.next_field == "drivers_license"
    assert answered.screening_status is ScreeningStatus.IN_PROGRESS
    assert interpreter.calls == 1
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.disclosure_acknowledged is True
    assert view.state.full_name is not None
    assert view.state.full_name.value == "Laura Pineda"


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
        [
            TurnInterpretation(full_name=ExtractedValue(value="Laura Pineda", provided=True)),
            TurnInterpretation(),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=language)

    # Lowercase formatting deliberately keeps this test on the model path so
    # it continues to model a provider response that omits disclosure.
    name_result = await coordinator.process_turn(created.conversation_id, "laura pineda", "name")
    assert name_result.next_field == "drivers_license"

    license_result = await coordinator.process_turn(created.conversation_id, affirmative, "license")
    assert license_result.screening_status is ScreeningStatus.IN_PROGRESS
    assert license_result.next_field == "location"
    location_prompt = license_result.assistant_message.casefold()
    assert ("city" in location_prompt) or ("ciudad" in location_prompt)
    assert interpreter.calls == 2

    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.drivers_license is not None
    assert view.state.drivers_license.value is True


@pytest.mark.asyncio
async def test_semantic_i_have_one_license_answer_is_true_and_advances(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A natural licence answer is applied from the typed semantic output."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(),
            TurnInterpretation(
                drivers_license=ExtractedValue(
                    value=True,
                    provided=True,
                    evidence="I have one",
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "Ada Lovelace", "name")
    result = await coordinator.process_turn(
        created.conversation_id,
        "I have one",
        "license-natural",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.LOCATION.value
    assert result.error_code is None
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.drivers_license is not None
    assert view.state.drivers_license.value is True
    assert interpreter.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Yep", "Yes, sorry"])
async def test_semantic_pending_madrid_city_affirmatives_confirm_and_advance(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    message: str,
) -> None:
    """Natural affirmative confirmations apply the pending city proposal."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(confirmation=True),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "Ada Lovelace", "name")
    await coordinator.process_turn(created.conversation_id, "yes", "license")
    offer = await coordinator.process_turn(created.conversation_id, "Madrid", "city")
    assert offer.next_field == ScreeningField.LOCATION.value

    accepted = await coordinator.process_turn(created.conversation_id, message, "city-confirm")

    assert accepted.screening_status is ScreeningStatus.IN_PROGRESS
    assert accepted.next_field == ScreeningField.AVAILABILITY.value
    assert accepted.error_code is None
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.city == "Madrid"
    assert view.state.location.confirmed is True
    assert view.state.location.service_area_ids
    assert interpreter.calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "I am looking for full-time",
        "I said I am looking for full-time, that is",
    ],
)
async def test_semantic_full_time_answers_update_availability_without_generic_error(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    message: str,
) -> None:
    """Natural availability answers remain typed and move to the next field."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(
                availability=ExtractedValue(
                    value=[AvailabilityType.FULL_TIME],
                    provided=True,
                    evidence=message,
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)
    await _advance_to_availability(coordinator, created.conversation_id)

    result = await coordinator.process_turn(created.conversation_id, message, "availability")

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.PREFERRED_SCHEDULE.value
    assert result.error_code is None
    assert "information" not in result.assistant_message.casefold()
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.availability is not None
    assert view.state.availability.value == [AvailabilityType.FULL_TIME]
    assert interpreter.calls == 4


@pytest.mark.asyncio
async def test_unsupported_salary_question_preserves_pending_availability_bridge(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """An unsupported FAQ answer bridges back without mutating availability."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(
                intent=TurnIntent.QUESTION,
                response_requested=True,
                candidate_questions=["What is the salary?"],
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)
    await _advance_to_availability(coordinator, created.conversation_id)
    before = await coordinator.get_conversation(created.conversation_id)

    result = await coordinator.process_turn(
        created.conversation_id,
        "What is the salary?",
        "salary-question",
    )

    after = await coordinator.get_conversation(created.conversation_id)
    assert before.state.current_field is ScreeningField.AVAILABILITY
    assert after.state.current_field is ScreeningField.AVAILABILITY
    assert after.state.availability is None
    assert after.state.clarification_counts == before.state.clarification_counts
    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.AVAILABILITY.value
    assert result.error_code is None
    assert "information" in result.assistant_message.casefold()
    assert "full-time" in result.assistant_message.casefold()
    assert interpreter.calls == 4


@pytest.mark.asyncio
async def test_labelled_multi_field_answer_uses_semantic_interpretation(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A semantic multi-field interpretation advances all canonical fields."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                detected_language=Language.EN,
                language_confidence=1.0,
                disclosure_acknowledged=True,
                full_name=ExtractedValue(
                    value="Isla Morgan", provided=True, evidence="Name: Isla Morgan"
                ),
                drivers_license=ExtractedValue(value=True, provided=True, evidence="licence: yes"),
                location=ExtractedLocation(
                    raw_value="Valencia centro",
                    city="Valencia",
                    zone="Ciutat Vella",
                    provided=True,
                    evidence="location: Valencia centro",
                ),
                availability=ExtractedValue(
                    value=[AvailabilityType.FULL_TIME],
                    provided=True,
                    evidence="availability: full time",
                ),
                preferred_schedule=ExtractedValue(
                    value=SchedulePreference.AFTERNOON,
                    provided=True,
                    evidence="schedule: afternoon",
                ),
                delivery_experience=ExtractedDeliveryExperience(
                    years=5,
                    platforms=["Deliveroo"],
                    provided=True,
                    evidence="experience: 5 years Deliveroo",
                ),
                start_availability=StartAvailabilityExtraction(
                    raw_value="next month",
                    precision="month",
                    provided=True,
                    evidence="start: next month",
                ),
            ),
            TurnInterpretation(final_confirmation=True),
            TurnInterpretation(confirmation=False),
        ]
    )
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
    assert "Isla Morgan" in first.assistant_message
    assert "Valencia" in first.assistant_message
    assert "5 years (Deliveroo)" in first.assistant_message
    assert "next month" in first.assistant_message
    assert interpreter.calls == 1

    confirmed = await coordinator.process_turn(created.conversation_id, "yes", "final-yes")
    assert confirmed.screening_status is ScreeningStatus.IN_PROGRESS
    assert "questions about the company" in confirmed.assistant_message
    assert confirmed.decision is not None
    assert confirmed.decision.reason_codes == ["awaiting_candidate_questions"]

    finished = await coordinator.process_turn(
        created.conversation_id,
        "No more questions",
        "faq-finished",
    )
    assert finished.screening_status is ScreeningStatus.QUALIFIED
    assert finished.decision is not None
    assert finished.decision.reason_codes == ["all_explicit_criteria_met"]
    assert finished.decision.rule_trace["decision_rule"] == "all_explicit_criteria_met"
    assert interpreter.calls == 3


@pytest.mark.asyncio
async def test_empty_model_start_answer_is_retained_and_shown_in_final_review(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A free-text start answer survives a valid but neutral model result."""

    _, build = coordinator_factory
    initial = _complete_interpretation("Leo Guzman").model_copy(
        update={"start_availability": None, "detected_language": Language.ES}
    )
    interpreter = QueueInterpreter([initial, TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    first = await coordinator.process_turn(
        created.conversation_id,
        (
            "Leo Guzman, licencia válida, Madrid centro, fines de semana, "
            "horario flexible y 15 años en Lift"
        ),
        "all-except-start",
    )
    assert first.next_field == ScreeningField.START_AVAILABILITY.value

    reviewed = await coordinator.process_turn(
        created.conversation_id,
        "Pues lo antes posible por favor",
        "start-verbatim",
    )

    assert reviewed.screening_status is ScreeningStatus.IN_PROGRESS
    assert reviewed.next_field is None
    assert "Leo Guzman" in reviewed.assistant_message
    assert "Pues lo antes posible por favor" in reviewed.assistant_message
    assert "¿Está todo correcto?" in reviewed.assistant_message
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.start_availability is not None
    assert view.state.start_availability.raw_value == "Pues lo antes posible por favor"
    assert interpreter.calls == 2


@pytest.mark.asyncio
async def test_post_screening_faq_answers_questions_then_closes_on_no_more_questions(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            _complete_interpretation("Grace Lee"),
            TurnInterpretation(final_confirmation=True),
            TurnInterpretation(
                intent=TurnIntent.QUESTION,
                response_requested=True,
                candidate_questions=["What schedules are available?"],
            ),
            TurnInterpretation(
                faq_complete=True,
                full_name=ExtractedValue(
                    value="Grace Lee",
                    provided=True,
                    evidence="canonical state echo",
                ),
                availability=ExtractedValue(
                    value=[AvailabilityType.FULL_TIME],
                    provided=True,
                    evidence="canonical state echo",
                ),
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(
        created.conversation_id,
        "Grace Lee, valid licence, Madrid centro, full-time, morning, 2 years, ASAP",
        "details",
    )
    offered = await coordinator.process_turn(created.conversation_id, "Yes", "confirm")
    assert offered.screening_status is ScreeningStatus.IN_PROGRESS
    assert "do you have any questions" in offered.assistant_message.casefold()
    offered_view = await coordinator.get_conversation(created.conversation_id)
    assert offered_view.status is ConversationStatus.ACTIVE

    answered = await coordinator.process_turn(
        created.conversation_id,
        "What schedules are available?",
        "faq-question",
    )
    assert answered.screening_status is ScreeningStatus.IN_PROGRESS
    assert "other questions" in answered.assistant_message.casefold()

    finished = await coordinator.process_turn(
        created.conversation_id,
        "No more questions",
        "faq-finished",
    )
    assert finished.screening_status is ScreeningStatus.QUALIFIED
    finished_view = await coordinator.get_conversation(created.conversation_id)
    assert finished_view.status is ConversationStatus.COMPLETED
    assert interpreter.calls == 4


@pytest.mark.asyncio
async def test_final_review_promotes_generic_model_confirmation_before_faq_offer(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            _complete_interpretation("Leo Guzman").model_copy(
                update={"detected_language": Language.ES}
            ),
            TurnInterpretation(
                confirmation=True,
                full_name=ExtractedValue(
                    value="Leo Guzman",
                    provided=True,
                    evidence="canonical state echo",
                ),
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    await coordinator.process_turn(
        created.conversation_id,
        "Leo Guzman, valid licence, Madrid centro, full-time, morning, 2 years, ASAP",
        "details",
    )
    result = await coordinator.process_turn(
        created.conversation_id,
        "Todo es correcto",
        "natural-final-confirmation",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.decision is not None
    assert result.decision.reason_codes == ["awaiting_candidate_questions"]
    assert "pregunta" in result.assistant_message.casefold()
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.candidate_confirmed is True
    assert view.state.faq_offer_made is True


@pytest.mark.asyncio
async def test_goal_aware_sufficiency_lets_exact_final_yes_override_echoed_fact(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            _complete_interpretation("Grace Lee"),
            TurnInterpretation(
                full_name=ExtractedValue(
                    value="Grace Lee",
                    provided=True,
                    evidence="canonical state echo",
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(
        created.conversation_id,
        "Grace Lee, valid licence, Madrid centro, full-time, morning, 2 years, ASAP",
        "details",
    )
    result = await coordinator.process_turn(created.conversation_id, "Yes", "exact-final-yes")

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.decision is not None
    assert result.decision.reason_codes == ["awaiting_candidate_questions"]
    assert "questions" in result.assistant_message.casefold()
    assert interpreter.calls == 3


@pytest.mark.asyncio
async def test_final_review_retries_semantically_when_first_model_output_only_echoes_state(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            _complete_interpretation("Leo Guzman").model_copy(
                update={"detected_language": Language.ES}
            ),
            TurnInterpretation(
                detected_language=Language.ES,
                full_name=ExtractedValue(
                    value="Leo Guzman",
                    provided=True,
                    evidence="canonical state echo",
                ),
            ),
            TurnInterpretation(
                detected_language=Language.ES,
                final_confirmation=True,
            ),
        ],
        usages=[{"requests": 1}, {"requests": 1}, {"requests": 1}],
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    await coordinator.process_turn(
        created.conversation_id,
        "Leo Guzman, valid licence, Madrid centro, full-time, morning, 2 years, ASAP",
        "details",
    )
    result = await coordinator.process_turn(
        created.conversation_id,
        "Todo es correcto",
        "semantic-final-retry",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.decision is not None
    assert result.decision.reason_codes == ["awaiting_candidate_questions"]
    assert "pregunta" in result.assistant_message.casefold()
    assert interpreter.calls == 3


@pytest.mark.asyncio
async def test_model_final_confirmation_cannot_bypass_deterministic_qualification(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A model confirmation flag cannot qualify a state with missing facts."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation(final_confirmation=True)])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(created.conversation_id, "yes", "premature-confirm")

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.decision is not None
    assert result.decision.reason_codes == ["required_information_missing"]
    assert result.decision.rule_trace["decision_rule"] == "required_fields"
    assert interpreter.calls == 1


@pytest.mark.asyncio
async def test_explicit_language_api_event_skips_provider(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """An explicit API language selector is a provider-free control event."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter([TurnInterpretation()])
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    result = await coordinator.process_turn(
        created.conversation_id,
        "English",
        "language-api-event",
        language=Language.EN,
    )

    assert result.status is TurnStatus.COMPLETED
    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.FULL_NAME.value
    assert interpreter.calls == 0
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.preferred_language is Language.EN
    usage = await _turn_usage(factory, created.conversation_id, "language-api-event")
    assert usage["llm_attempted"] is False
    assert usage["llm_succeeded"] is False
    assert usage["llm_skipped_reason"] == "explicit_language_event"
    assert usage["deterministic_fallback_attempted"] is False


@pytest.mark.asyncio
async def test_exact_opt_out_control_skips_provider(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """An exact opt-out remains an application-owned provider-free control."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [TurnInterpretation(full_name=ExtractedValue(value="should not be used", provided=True))]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(created.conversation_id, "stop", "exact-opt-out")

    assert result.status is TurnStatus.COMPLETED
    assert result.screening_status is ScreeningStatus.ABANDONED
    assert result.decision is not None
    assert result.decision.reason_codes == ["candidate_opted_out"]
    assert interpreter.calls == 0
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.full_name is None
    usage = await _turn_usage(factory, created.conversation_id, "exact-opt-out")
    assert usage["llm_attempted"] is False
    assert usage["llm_succeeded"] is False
    assert usage["llm_skipped_reason"] == "exact_opt_out"
    assert usage["deterministic_fallback_attempted"] is False


@pytest.mark.asyncio
async def test_explicit_language_switch_is_model_first_with_deterministic_recovery(
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
    assert interpreter.calls == 2

    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.preferred_language is Language.EN


@pytest.mark.asyncio
async def test_question_is_answered_or_bounded_before_screening_resumes(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(
                intent=TurnIntent.QUESTION,
                response_requested=True,
                candidate_questions=["What schedules are available?"],
            ),
            TurnInterpretation(
                intent=TurnIntent.QUESTION,
                response_requested=True,
                candidate_questions=["What is the salary?"],
            ),
        ]
    )
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
    assert interpreter.calls == 2


@pytest.mark.asyncio
async def test_spanish_faq_first_questions_are_recovered_and_answered(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """Missing question arrays cannot make FAQ-first turns look like answers."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(intent=TurnIntent.QUESTION),
            TurnInterpretation(
                intent=TurnIntent.QUESTION,
                response_requested=True,
                # Simulate the provider echo observed with bounded history.
                candidate_questions=["¿En qué consiste el puesto?"],
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    role = await coordinator.process_turn(
        created.conversation_id,
        "¿En qué consiste el puesto?",
        "faq-role-first",
    )
    process = await coordinator.process_turn(
        created.conversation_id,
        "¿Cómo es el proceso?",
        "faq-process-second",
    )

    assert role.next_field == ScreeningField.FULL_NAME.value
    assert "recoger pedidos" in role.assistant_message
    assert "nombre completo" in role.assistant_message
    assert process.next_field == ScreeningField.FULL_NAME.value
    assert "Primero recogemos datos" in process.assistant_message
    assert "nombre completo" in process.assistant_message
    assert interpreter.calls == 2
    role_usage = await _turn_usage(factory, created.conversation_id, "faq-role-first")
    process_usage = await _turn_usage(factory, created.conversation_id, "faq-process-second")
    assert role_usage["candidate_question_recovered"] is True
    assert process_usage["candidate_question_grounded"] is True


@pytest.mark.asyncio
async def test_off_topic_message_is_bounded_after_semantic_interpretation(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [TurnInterpretation(intent=TurnIntent.OFF_TOPIC, response_requested=True)]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    result = await coordinator.process_turn(created.conversation_id, "Tell me a joke", "off-topic")
    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == "full_name"
    assert "don’t have that information" in result.assistant_message
    assert interpreter.calls == 1


@pytest.mark.asyncio
async def test_known_location_mislabelled_as_name_is_reconciled_as_location(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ana Ruiz", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
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
async def test_exact_location_alias_is_model_first_and_reconciled(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    message: str,
    expected_area: str,
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            # The exact aliases are reconciled deterministically only after
            # the provider has had an opportunity to interpret the message.
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
    assert interpreter.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "city_answer", "expected_prompt_fragment"),
    [
        (Language.EN, "Madrid", "Can you deliver"),
        (Language.ES, "Madrid", "¿Puedes repartir"),
    ],
)
async def test_known_city_location_creates_safe_pending_city_offer_after_model_attempt(
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
    assert interpreter.calls == 3


@pytest.mark.asyncio
async def test_known_city_is_recovered_when_model_requests_response_and_echoes_bad_evidence(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A provider flag cannot suppress a catalogue-grounded city answer."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Carlos Alcantara", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(
                response_requested=True,
                location=ExtractedLocation(
                    raw_value="Madrid",
                    city="Madrid",
                    provided=True,
                    evidence="turn:stale-canonical-evidence",
                ),
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    await coordinator.process_turn(created.conversation_id, "Carlos Alcantara", "name")
    await coordinator.process_turn(created.conversation_id, "Sí, tengo una", "licence")
    offer = await coordinator.process_turn(created.conversation_id, "Madrid", "city")

    assert offer.screening_status is ScreeningStatus.IN_PROGRESS
    assert offer.next_field == ScreeningField.LOCATION.value
    assert "Centro" in offer.assistant_message
    assert "Salamanca" in offer.assistant_message
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.location.city == "Madrid"
    assert view.state.pending_confirmation is not None
    assert view.state.pending_confirmation.reason == "service_area_city"
    usage = await _turn_usage(factory, created.conversation_id, "city")
    assert usage["location_patch_discarded"] is True
    assert usage["deterministic_fallback_reason"] == "message_exact"


@pytest.mark.asyncio
async def test_exact_yes_accepts_city_offer_despite_model_stale_location_echo(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A stale provider echo cannot make an exact pending confirmation loop."""

    factory, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Luis Sainz", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(
                location=ExtractedLocation(
                    raw_value="Madrid",
                    city="Madrid",
                    provided=True,
                    evidence="Madrid",
                )
            ),
            TurnInterpretation(
                response_requested=True,
                location=ExtractedLocation(
                    raw_value="Madrid",
                    city="Madrid",
                    provided=True,
                    evidence="turn:stale-canonical-evidence",
                ),
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.ES)

    await coordinator.process_turn(created.conversation_id, "Luis Sainz", "name")
    await coordinator.process_turn(created.conversation_id, "Sí tengo una", "licence")
    offer = await coordinator.process_turn(created.conversation_id, "Madrid", "city")
    assert offer.next_field == ScreeningField.LOCATION.value

    accepted = await coordinator.process_turn(created.conversation_id, "Sí", "city-confirmation")

    assert accepted.screening_status is ScreeningStatus.IN_PROGRESS
    assert accepted.next_field == ScreeningField.AVAILABILITY.value
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.confirmed is True
    assert view.state.location.service_area_ids == [
        "es-mad-centro",
        "es-mad-salamanca",
    ]
    usage = await _turn_usage(factory, created.conversation_id, "city-confirmation")
    assert usage["location_patch_discarded"] is True
    assert usage["deterministic_fallback_used"] is True
    assert usage["deterministic_fallback_reason"] == "confirmation"


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
async def test_pending_city_zone_answer_is_resolved_after_model_attempt(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
            # A trusted pending-city zone answer is resolved after the
            # provider returns an empty response.
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
    assert interpreter.calls == 4


@pytest.mark.asyncio
async def test_natural_pending_city_zone_answer_from_provider_is_canonicalized(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    """A semantic ``Centro is fine`` answer selects the offered Madrid zone."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(full_name=ExtractedValue(value="Ada Lovelace", provided=True)),
            TurnInterpretation(drivers_license=ExtractedValue(value=True, provided=True)),
            TurnInterpretation(location=ExtractedLocation(raw_value="Madrid", provided=True)),
            TurnInterpretation(
                location=ExtractedLocation(
                    # Providers often retain only the concrete zone while the
                    # evidence contains the candidate's full sentence.
                    raw_value="Centro",
                    provided=True,
                    evidence="Centro is fine",
                )
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)

    await coordinator.process_turn(created.conversation_id, "Ada Lovelace", "name")
    await coordinator.process_turn(created.conversation_id, "yes", "licence")
    offer = await coordinator.process_turn(created.conversation_id, "Madrid", "city")
    assert offer.next_field == ScreeningField.LOCATION.value

    selected = await coordinator.process_turn(
        created.conversation_id,
        "Centro is fine",
        "natural-zone-provider",
    )

    assert selected.screening_status is ScreeningStatus.IN_PROGRESS
    assert selected.next_field == ScreeningField.AVAILABILITY.value
    assert selected.error_code is None
    assert "service area" not in selected.assistant_message.casefold()
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.service_area_id == "es-mad-centro"
    assert view.state.location.confirmed is True
    assert interpreter.calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("provided", [True, False])
async def test_stale_location_patch_does_not_override_availability_answer(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
    provided: bool,
) -> None:
    """A provider's echoed location cannot create a false correction prompt."""

    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(),
            TurnInterpretation(
                availability=ExtractedValue(
                    value=[AvailabilityType.WEEKENDS],
                    provided=True,
                    evidence="only weekends please",
                ),
                # This stale patch reproduces the observed model output after
                # Madrid Centro had already been accepted.
                location=ExtractedLocation(
                    raw_value="Centro",
                    city="Madrid",
                    zone="Centro",
                    provided=provided,
                    evidence="Centro",
                ),
            ),
        ]
    )
    coordinator = build(interpreter)
    created = await coordinator.create_conversation(language=Language.EN)
    await _advance_to_availability(coordinator, created.conversation_id)

    result = await coordinator.process_turn(
        created.conversation_id,
        "only weekends please",
        "availability-stale-location",
    )

    assert result.screening_status is ScreeningStatus.IN_PROGRESS
    assert result.next_field == ScreeningField.PREFERRED_SCHEDULE.value
    assert result.error_code is None
    assert "service area" not in result.assistant_message.casefold()
    view = await coordinator.get_conversation(created.conversation_id)
    assert view.state.pending_confirmation is None
    assert view.state.location.service_area_id == "es-mad-centro"
    assert view.state.availability is not None
    assert view.state.availability.value == [AvailabilityType.WEEKENDS]
    assert interpreter.calls == 4


@pytest.mark.asyncio
async def test_explicit_name_correction_is_confirmed_after_semantic_interpretation(
    coordinator_factory: tuple[async_sessionmaker[AsyncSession], CoordinatorBuilder],
) -> None:
    _, build = coordinator_factory
    interpreter = QueueInterpreter(
        [
            _complete_interpretation("Riley Stone"),
            TurnInterpretation(
                intent=TurnIntent.CORRECTION,
                full_name=ExtractedValue(
                    value="Riley Jones",
                    provided=True,
                    correction=True,
                    evidence="my full name is Riley Jones",
                ),
            ),
            TurnInterpretation(confirmation=True),
            TurnInterpretation(final_confirmation=True),
            TurnInterpretation(faq_complete=True),
        ]
    )
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
    assert interpreter.calls == 2

    accepted = await coordinator.process_turn(created.conversation_id, "yes", "accept")
    assert accepted.screening_status is ScreeningStatus.IN_PROGRESS
    assert accepted.next_field is None
    final = await coordinator.process_turn(created.conversation_id, "yes", "final")
    assert final.screening_status is ScreeningStatus.IN_PROGRESS
    assert "questions about the company" in final.assistant_message
    finished = await coordinator.process_turn(
        created.conversation_id,
        "No more questions",
        "faq-finished",
    )
    assert finished.screening_status is ScreeningStatus.QUALIFIED
    assert interpreter.calls == 5
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
