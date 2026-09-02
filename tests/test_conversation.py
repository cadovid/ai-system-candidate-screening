from __future__ import annotations

from candidate_screening.ai.schemas import ExtractedValue, TurnInterpretation
from candidate_screening.application import ConversationController
from candidate_screening.domain import Language, ScreeningEngine, ScreeningState, ScreeningStatus
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
