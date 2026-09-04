from __future__ import annotations

from pathlib import Path

from candidate_screening.ai.schemas import (
    ExtractedLocation,
    ExtractedValue,
    TurnIntent,
    TurnInterpretation,
)
from candidate_screening.application import ConversationController, FAQCatalog
from candidate_screening.domain import (
    Language,
    LocationMatchStatus,
    ScreeningEngine,
    ScreeningField,
    ScreeningState,
    ScreeningStatus,
)
from candidate_screening.domain.service_areas import ServiceAreaMatcher


def test_controller_localizes_prompt_and_handles_opt_out(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    result = controller.process(ScreeningState.empty(Language.EN), TurnInterpretation())
    assert result.decision.status is ScreeningStatus.IN_PROGRESS
    assert "full name" in result.assistant_message.lower()

    opted_out = controller.process(
        result.state,
        TurnInterpretation(opt_out_requested=True),
    )
    assert opted_out.opt_out is True
    assert opted_out.decision.status is ScreeningStatus.ABANDONED
    assert "close" in opted_out.assistant_message.lower()


def test_prompt_injection_cannot_change_state(service_area_matcher: ServiceAreaMatcher) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    state = ScreeningState.empty(Language.ES)
    result = controller.process(
        state,
        TurnInterpretation(
            prompt_injection_detected=True,
            full_name=ExtractedValue(value="Injected", provided=True),
        ),
    )
    assert result.security_event is True
    assert result.state == state
    assert result.state.full_name is None


def test_clarification_limit_hands_off_without_mutating_trusted_value(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(
        ScreeningEngine(), service_area_matcher, max_clarifications=2
    )
    state = ScreeningState.empty(Language.EN)
    invalid = TurnInterpretation(
        full_name=ExtractedValue(provided=True, ambiguous=True, evidence="unclear")
    )
    first = controller.process(state, invalid)
    assert first.decision.status is ScreeningStatus.IN_PROGRESS
    second = controller.process(first.state, invalid)
    assert second.decision.status is ScreeningStatus.NEEDS_REVIEW
    assert second.state.full_name is None


def test_known_city_prompts_for_any_configured_area_and_accepts_yes(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    offered = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
        ),
    )
    assert offered.decision.status is ScreeningStatus.IN_PROGRESS
    assert offered.decision.reason_codes == ["ambiguous_location"]
    assert offered.state.pending_confirmation is not None
    assert offered.state.pending_confirmation.reason == "service_area_city"
    assert "Madrid" in offered.assistant_message
    assert "Centro" in offered.assistant_message
    assert "Salamanca" in offered.assistant_message
    assert "yes or no" in offered.assistant_message

    accepted = controller.process(
        offered.state,
        TurnInterpretation(confirmation=True),
    )
    assert accepted.state.pending_confirmation is None
    assert accepted.state.location.match_status is LocationMatchStatus.EXACT
    assert accepted.state.location.confirmed is True
    assert accepted.state.location.service_area_id is None
    assert accepted.state.location.service_area_ids == [
        "es-mad-centro",
        "es-mad-salamanca",
    ]
    assert accepted.next_field is ScreeningField.FULL_NAME


def test_known_city_negative_answer_is_disqualified_with_interpreted_location(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    offered = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
        ),
    )
    rejected = controller.process(offered.state, TurnInterpretation(confirmation=False))
    assert rejected.decision.status is ScreeningStatus.DISQUALIFIED
    assert rejected.decision.reason_codes == ["outside_service_area"]
    assert "Madrid" in rejected.assistant_message
    assert "Centro" in rejected.assistant_message
    assert "Salamanca" in rejected.assistant_message
    assert rejected.state.location.match_status is LocationMatchStatus.UNSUPPORTED


def test_city_offer_is_repeated_after_non_confirmation_without_hiding_the_options(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    offered = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
        ),
    )
    repeated = controller.process(offered.state, TurnInterpretation())
    assert repeated.decision.status is ScreeningStatus.IN_PROGRESS
    assert repeated.state.pending_confirmation is not None
    assert "Centro" in repeated.assistant_message
    assert "Salamanca" in repeated.assistant_message
    assert "yes or no" in repeated.assistant_message


def test_known_city_offer_accepts_a_concrete_configured_zone(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    offered = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
        ),
    )
    selected = controller.process(
        offered.state,
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="Madrid Centro", provided=True, evidence="Madrid Centro"
            )
        ),
    )
    assert selected.state.pending_confirmation is None
    assert selected.state.location.match_status is LocationMatchStatus.EXACT
    assert selected.state.location.service_area_id == "es-mad-centro"
    assert selected.state.location.service_area_ids == []


def test_known_city_offer_accepts_short_zone_with_structured_city_context(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    offered = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
        ),
    )
    selected = controller.process(
        offered.state,
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="city center",
                city="Madrid",
                provided=True,
                evidence="city center",
            )
        ),
    )
    assert selected.state.pending_confirmation is None
    assert selected.state.location.match_status is LocationMatchStatus.EXACT
    assert selected.state.location.service_area_id == "es-mad-centro"


def test_language_switch_does_not_consume_pending_city_confirmation(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    offered = controller.process(
        ScreeningState.empty(Language.ES),
        TurnInterpretation(
            location=ExtractedLocation(raw_value="Madrid", provided=True, evidence="Madrid")
        ),
    )
    switched = controller.process(
        offered.state,
        TurnInterpretation(explicit_language=Language.EN),
    )
    assert switched.state.preferred_language is Language.EN
    assert switched.state.pending_confirmation is not None
    assert switched.state.clarification_counts.get(ScreeningField.LOCATION, 0) == 0
    assert switched.decision.status is ScreeningStatus.IN_PROGRESS
    assert "Can you deliver" in switched.assistant_message


def test_english_city_center_phrase_reaches_exact_area_reconciliation(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    result = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="The city center of Madrid",
                city="Madrid",
                zone="city center",
                provided=True,
                evidence="The city center of Madrid",
            )
        ),
    )
    assert result.decision.status is ScreeningStatus.IN_PROGRESS
    assert result.state.location.service_area_id == "es-mad-centro"
    assert result.state.location.match_status is LocationMatchStatus.EXACT
    assert result.state.pending_confirmation is None
    assert "outside" not in result.assistant_message.lower()


def test_unsupported_location_message_echoes_exact_interpreted_area(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    result = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="Bilbao city centre", provided=True, evidence="Bilbao city centre"
            )
        ),
    )
    assert result.decision.status is ScreeningStatus.DISQUALIFIED
    assert "Bilbao city centre" in result.assistant_message


def test_faq_question_is_answered_and_screening_prompt_resumes(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    catalog = FAQCatalog.from_file(Path("data/faq/faq.json"))
    controller = ConversationController(
        ScreeningEngine(), service_area_matcher, faq_catalog=catalog
    )
    result = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            intent=TurnIntent.QUESTION,
            response_requested=True,
            candidate_questions=["What schedules are available?"],
        ),
    )
    assert result.faq_answered is True
    assert "Morning" in result.assistant_message
    assert "What is your full name?" in result.assistant_message
    assert result.next_field is ScreeningField.FULL_NAME


def test_unknown_question_is_bounded_and_does_not_mutate_screening_state(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    catalog = FAQCatalog.from_file(Path("data/faq/faq.json"))
    controller = ConversationController(
        ScreeningEngine(), service_area_matcher, faq_catalog=catalog
    )
    state = ScreeningState.empty(Language.EN)
    result = controller.process(
        state,
        TurnInterpretation(
            intent=TurnIntent.QUESTION,
            response_requested=True,
            candidate_questions=["What is the salary?"],
        ),
    )
    assert result.faq_answered is False
    assert "don’t have that information" in result.assistant_message
    assert result.state.full_name is None
    assert result.next_field is ScreeningField.FULL_NAME


def test_off_topic_message_is_acknowledged_before_resuming_screening(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    controller = ConversationController(ScreeningEngine(), service_area_matcher)
    result = controller.process(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(intent=TurnIntent.OFF_TOPIC, response_requested=True),
    )
    assert result.next_field is ScreeningField.FULL_NAME
    assert "don’t have that information" in result.assistant_message
