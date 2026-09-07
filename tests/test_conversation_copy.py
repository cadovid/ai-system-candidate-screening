from __future__ import annotations

from candidate_screening.application.conversation_copy import render_response_plan
from candidate_screening.application.response_plan import ResponseKind, ResponsePlan
from candidate_screening.domain.enums import (
    AvailabilityType,
    Language,
    SchedulePreference,
    ScreeningField,
)
from candidate_screening.domain.models import (
    DeliveryExperience,
    LocationState,
    PendingConfirmation,
    ScreeningState,
    SourcedValue,
    StartAvailability,
)


def _plan(kind: ResponseKind) -> ResponsePlan:
    return ResponsePlan(
        kind=kind,
        language=Language.EN,
        field=ScreeningField.FULL_NAME,
        variant_key=f"copy-test:{kind.value}",
    )


def _pending_plan(
    field: ScreeningField,
    proposed_value: object,
    *,
    reason: str = "candidate_correction",
    language: Language = Language.EN,
) -> ResponsePlan:
    return ResponsePlan(
        kind=ResponseKind.PENDING_CONFIRMATION,
        language=language,
        field=field,
        pending=PendingConfirmation(
            field=field,
            proposed_value=proposed_value,
            reason=reason,
        ),
    )


def test_simple_prompt_has_three_bounded_natural_variants() -> None:
    outputs = {
        render_response_plan(
            _plan(ResponseKind.PROMPT),
            selector=lambda key, variant_count, index=index: index,
        )
        for index in range(3)
    }

    assert len(outputs) == 3
    assert all("full name" in output for output in outputs)
    assert all(len(output) < 120 for output in outputs)


def test_clarification_has_three_bounded_natural_variants() -> None:
    outputs = {
        render_response_plan(
            _plan(ResponseKind.CLARIFICATION),
            selector=lambda key, variant_count, index=index: index,
        )
        for index in range(3)
    }

    assert len(outputs) == 3
    assert all("full name" in output.lower() for output in outputs)
    assert all(len(output) < 180 for output in outputs)


def test_terminal_copy_is_not_variant_rendered() -> None:
    plan = ResponsePlan(kind=ResponseKind.OPT_OUT, language=Language.EN)

    assert (
        render_response_plan(plan)
        == "Understood. We’ll close this screening. Thank you for your time."
    )


def test_final_confirmation_lists_the_canonical_screening_values() -> None:
    state = ScreeningState(
        full_name=SourcedValue(value="Leo Guzman"),
        drivers_license=SourcedValue(value=True),
        location=LocationState(
            raw_value="Madrid centro",
            matched_name="Madrid — Centro",
            confirmed=True,
        ),
        availability=SourcedValue(value=[AvailabilityType.WEEKENDS]),
        preferred_schedule=SourcedValue(value=SchedulePreference.FLEXIBLE),
        delivery_experience=DeliveryExperience(years=15, platforms=["Lift"]),
        start_availability=StartAvailability(raw_value="lo antes posible"),
        preferred_language=Language.ES,
        current_field=None,
    )
    plan = ResponsePlan(
        kind=ResponseKind.FINAL_CONFIRMATION,
        language=Language.ES,
        state=state,
    )

    message = render_response_plan(plan)

    assert "Leo Guzman" in message
    assert "Madrid — Centro" in message
    assert "fines de semana" in message
    assert "flexible" in message
    assert "15 años (Lift)" in message
    assert "lo antes posible" in message
    assert message.endswith("¿Está todo correcto?")


def test_decision_impact_location_confirmation_describes_proposed_area() -> None:
    plan = _pending_plan(
        ScreeningField.LOCATION,
        {"raw_value": "Bilbao city centre", "city": "Bilbao", "zone": "Abando"},
        reason="decision_impact_confirmation",
    )

    message = render_response_plan(plan)

    assert "Bilbao city centre" in message
    assert "Bilbao" in message
    assert "Abando" in message
    assert "service area" in message
    assert "yes or no" in message


def test_decision_impact_license_confirmation_states_negative_answer() -> None:
    plan = _pending_plan(
        ScreeningField.DRIVERS_LICENSE,
        {"value": False},
        reason="decision_impact_confirmation",
    )

    message = render_response_plan(plan)

    assert "do not have a valid driver's licence" in message
    assert "Reply yes or no" in message


def test_decision_impact_confirmation_is_localized_in_spanish() -> None:
    plan = _pending_plan(
        ScreeningField.LOCATION,
        {"raw_value": "Bilbao centro", "city": "Bilbao", "zone": "Abando"},
        reason="decision_impact_confirmation",
        language=Language.ES,
    )

    message = render_response_plan(plan)

    assert "He entendido" in message
    assert "Bilbao centro" in message
    assert "Abando" in message
    assert "Responde sí o no" in message


def test_generic_correction_confirmation_describes_bounded_replacement() -> None:
    plan = _pending_plan(
        ScreeningField.FULL_NAME,
        {"value": "Riley Jones", "evidence": "private source text"},
    )

    message = render_response_plan(plan)

    assert "Riley Jones" in message
    assert "replace the previous information" in message
    assert "private source text" not in message
