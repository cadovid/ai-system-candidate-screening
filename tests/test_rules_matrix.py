from __future__ import annotations

from candidate_screening.domain.enums import (
    AvailabilityType,
    LocationMatchStatus,
    SchedulePreference,
    ScreeningField,
    ScreeningStatus,
)
from candidate_screening.domain.models import (
    DeliveryExperience,
    LocationState,
    PendingConfirmation,
    ScreeningState,
    SourcedValue,
    StartAvailability,
)
from candidate_screening.domain.rules import ScreeningEngine, evaluate_screening


def _complete(*, confirmed: bool = False) -> ScreeningState:
    return ScreeningState(
        full_name=SourcedValue(value="Ada Lovelace"),
        drivers_license=SourcedValue(value=True),
        location=LocationState(
            raw_value="Madrid centro",
            normalized_value="madrid centro",
            service_area_id="es-mad-centro",
            matched_name="Madrid — Centro",
            match_status=LocationMatchStatus.EXACT,
            confirmed=True,
        ),
        availability=SourcedValue(value=[AvailabilityType.FULL_TIME]),
        preferred_schedule=SourcedValue(value=SchedulePreference.MORNING),
        delivery_experience=DeliveryExperience(years=2),
        start_availability=StartAvailability(raw_value="ASAP"),
        candidate_confirmed=confirmed,
    )


def test_rules_return_stable_reason_codes_for_every_decision_branch() -> None:
    empty = evaluate_screening(ScreeningState.empty())
    assert empty.status is ScreeningStatus.IN_PROGRESS
    assert empty.reason_codes == ["required_information_missing"]
    assert empty.missing_fields[0] is ScreeningField.FULL_NAME

    no_license = _complete()
    no_license.drivers_license = SourcedValue(value=False)
    decision = evaluate_screening(no_license)
    assert decision.status is ScreeningStatus.DISQUALIFIED
    assert decision.reason_codes == ["no_drivers_license"]
    assert decision.rule_trace["decision_rule"] == "drivers_license_required"

    unsupported = _complete()
    unsupported.location = LocationState(match_status=LocationMatchStatus.UNSUPPORTED)
    decision = evaluate_screening(unsupported)
    assert decision.status is ScreeningStatus.DISQUALIFIED
    assert decision.reason_codes == ["outside_service_area"]

    ambiguous = _complete()
    ambiguous.location.match_status = LocationMatchStatus.AMBIGUOUS
    decision = evaluate_screening(ambiguous)
    assert decision.status is ScreeningStatus.IN_PROGRESS
    assert decision.reason_codes == ["ambiguous_location"]
    assert decision.issues[0].field is ScreeningField.LOCATION

    ambiguous_pending = _complete()
    ambiguous_pending.location.match_status = LocationMatchStatus.AMBIGUOUS
    ambiguous_pending.pending_confirmation = PendingConfirmation(
        field=ScreeningField.LOCATION, proposed_value={"service_area_id": "es-mad-centro"}
    )
    decision = evaluate_screening(ambiguous_pending)
    assert decision.status is ScreeningStatus.IN_PROGRESS
    assert decision.reason_codes == ["awaiting_confirmation"]

    pending = _complete()
    pending.pending_confirmation = PendingConfirmation(
        field=ScreeningField.PREFERRED_SCHEDULE, proposed_value={"value": "evening"}
    )
    decision = evaluate_screening(pending)
    assert decision.status is ScreeningStatus.IN_PROGRESS
    assert decision.reason_codes == ["awaiting_confirmation"]

    complete_unconfirmed = evaluate_screening(_complete())
    assert complete_unconfirmed.reason_codes == ["awaiting_candidate_confirmation"]
    assert complete_unconfirmed.rule_trace["decision_rule"] == "final_confirmation"

    qualified = evaluate_screening(_complete(confirmed=True))
    assert qualified.status is ScreeningStatus.QUALIFIED
    assert qualified.reason_codes == ["all_explicit_criteria_met"]


def test_rules_validate_location_confirmation_and_engine_ruleset_version() -> None:
    missing_service_id = _complete(confirmed=True)
    missing_service_id.location.service_area_id = None
    decision = evaluate_screening(missing_service_id)
    assert decision.status is ScreeningStatus.IN_PROGRESS
    assert ScreeningField.LOCATION in decision.missing_fields

    engine = ScreeningEngine(ruleset_version="test-rules")
    decision = engine.evaluate(ScreeningState.empty())
    assert decision.ruleset_version == "test-rules"
    assert engine.next_field(ScreeningState.empty()) is ScreeningField.FULL_NAME
