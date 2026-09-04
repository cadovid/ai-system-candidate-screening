from __future__ import annotations

from candidate_screening.ai.schemas import (
    ExtractedLocation,
    ExtractedValue,
    TurnInterpretation,
)
from candidate_screening.application.reconciliation import reconcile_interpretation
from candidate_screening.domain import (
    AvailabilityType,
    DeliveryExperience,
    Language,
    LocationMatchStatus,
    LocationState,
    SchedulePreference,
    ScreeningField,
    ScreeningState,
    ScreeningStatus,
    SourcedValue,
    StartAvailability,
    evaluate_screening,
)
from candidate_screening.domain.service_areas import ServiceAreaMatcher


def complete_state() -> ScreeningState:
    return ScreeningState(
        full_name=SourcedValue(value="Ana García"),
        drivers_license=SourcedValue(value=True),
        location=LocationState(
            raw_value="Madrid Centro",
            normalized_value="madrid centro",
            service_area_id="es-mad-centro",
            matched_name="Madrid — Centro",
            match_status=LocationMatchStatus.EXACT,
            confirmed=True,
        ),
        availability=SourcedValue(value=[AvailabilityType.FULL_TIME]),
        preferred_schedule=SourcedValue(value=SchedulePreference.FLEXIBLE),
        delivery_experience=DeliveryExperience(years=2),
        start_availability=StartAvailability(raw_value="ASAP"),
        candidate_confirmed=True,
    )


def test_only_explicit_license_and_area_criteria_disqualify_or_qualify() -> None:
    qualified = evaluate_screening(complete_state())
    assert qualified.status is ScreeningStatus.QUALIFIED

    no_license = complete_state().model_copy(deep=True)
    no_license.drivers_license = SourcedValue(value=False)
    assert evaluate_screening(no_license).status is ScreeningStatus.DISQUALIFIED

    unsupported = complete_state().model_copy(deep=True)
    unsupported.location = LocationState(
        raw_value="Paris",
        normalized_value="paris",
        match_status=LocationMatchStatus.UNSUPPORTED,
    )
    assert evaluate_screening(unsupported).status is ScreeningStatus.DISQUALIFIED

    forged_exact = complete_state().model_copy(deep=True)
    forged_exact.location.service_area_id = None
    decision = evaluate_screening(forged_exact)
    assert decision.status is ScreeningStatus.IN_PROGRESS
    assert ScreeningField.LOCATION in decision.missing_fields


def test_confirmed_city_scope_is_eligible_without_selecting_a_zone() -> None:
    state = complete_state().model_copy(deep=True)
    state.location = LocationState(
        raw_value="Madrid",
        normalized_value="madrid",
        city="Madrid",
        service_area_ids=["es-mad-centro", "es-mad-salamanca"],
        matched_name="Madrid — any configured area (Centro, Salamanca)",
        match_status=LocationMatchStatus.EXACT,
        confirmed=True,
    )
    assert evaluate_screening(state).status is ScreeningStatus.QUALIFIED


def test_service_area_exact_lookup_handles_duplicate_alias_entries(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    match = service_area_matcher.match("Málaga centro")
    assert match.status is LocationMatchStatus.EXACT
    assert match.area is not None
    assert match.area.id == "es-mal-centro"

    ambiguous = service_area_matcher.match("Madrid")
    assert ambiguous.status is LocationMatchStatus.AMBIGUOUS
    assert {area.id for area in ambiguous.suggestions} == {
        "es-mad-centro",
        "es-mad-salamanca",
    }


def test_reconciliation_stores_evidence_and_does_not_overwrite_corrections(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    state = ScreeningState.empty(Language.EN)
    first = reconcile_interpretation(
        state,
        TurnInterpretation(
            detected_language=Language.EN,
            language_confidence=1,
            full_name=ExtractedValue(value="Ana García", provided=True, evidence="Ana García"),
        ),
        service_area_matcher=service_area_matcher,
        message_id="m-1",
    )
    assert first.state.full_name is not None
    assert first.state.full_name.evidence.message_id == "m-1"

    correction = reconcile_interpretation(
        first.state,
        TurnInterpretation(
            full_name=ExtractedValue(
                value="Beatriz García", provided=True, correction=True, evidence="Actually Beatriz"
            )
        ),
        service_area_matcher=service_area_matcher,
        message_id="m-2",
    )
    assert correction.state.full_name is not None
    assert correction.state.full_name.value == "Ana García"
    assert correction.state.pending_confirmation is not None
    assert correction.state.pending_confirmation.field is ScreeningField.FULL_NAME

    accepted = reconcile_interpretation(
        correction.state,
        TurnInterpretation(confirmation=True),
        service_area_matcher=service_area_matcher,
    )
    assert accepted.state.full_name is not None
    assert accepted.state.full_name.value == "Beatriz García"
    assert accepted.state.pending_confirmation is None


def test_reconciliation_uses_deterministic_service_area_suggestion(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    result = reconcile_interpretation(
        ScreeningState.empty(),
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="Madrd centro", provided=True, evidence="Madrd centro"
            )
        ),
        service_area_matcher=service_area_matcher,
    )
    assert result.state.location.match_status is LocationMatchStatus.NEEDS_CONFIRMATION
    assert result.state.pending_confirmation is not None
    assert result.state.pending_confirmation.field is ScreeningField.LOCATION


def test_reconciliation_repairs_catalog_location_with_default_provided_flag(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    result = reconcile_interpretation(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="Madrid centro",
                city="Madrid",
                zone="Centro",
                provided=False,
                evidence="Madrid centro",
            )
        ),
        service_area_matcher=service_area_matcher,
    )

    assert result.state.location.service_area_id == "es-mad-centro"
    assert result.state.location.match_status is LocationMatchStatus.EXACT
    assert result.state.location.confirmed is True


def test_reconciliation_does_not_repair_contradictory_location_evidence(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    result = reconcile_interpretation(
        ScreeningState.empty(Language.EN),
        TurnInterpretation(
            location=ExtractedLocation(
                raw_value="Madrid centro",
                city="Madrid",
                zone="Centro",
                provided=False,
                evidence="Bilbao",
            )
        ),
        service_area_matcher=service_area_matcher,
    )

    assert result.state.location.service_area_id is None
    assert result.state.location.match_status is LocationMatchStatus.UNRESOLVED
