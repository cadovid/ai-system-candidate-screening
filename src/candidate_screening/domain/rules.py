"""Deterministic eligibility engine.

No model output is accepted as a status. The engine consumes only canonical
state and can be rerun from an audit record to reproduce the decision.
"""

from __future__ import annotations

from typing import Any

from .enums import LocationMatchStatus, ReviewReason, ScreeningField, ScreeningStatus
from .models import ScreeningDecision, ScreeningState, ValidationIssue

RULESET_VERSION = "2026-01"
REQUIRED_FIELDS: tuple[ScreeningField, ...] = (
    ScreeningField.FULL_NAME,
    ScreeningField.DRIVERS_LICENSE,
    ScreeningField.LOCATION,
    ScreeningField.AVAILABILITY,
    ScreeningField.PREFERRED_SCHEDULE,
    ScreeningField.DELIVERY_EXPERIENCE,
    ScreeningField.START_AVAILABILITY,
)


def _missing_fields(state: ScreeningState) -> list[ScreeningField]:
    missing: list[ScreeningField] = []
    if state.full_name is None:
        missing.append(ScreeningField.FULL_NAME)
    if state.drivers_license is None:
        missing.append(ScreeningField.DRIVERS_LICENSE)
    # A location is eligible only when the deterministic matcher identified a
    # concrete catalogue entry (or an explicitly confirmed set of configured
    # areas for a city) and the candidate confirmed it. ``EXACT`` by itself is
    # not enough: callers must not be able to manufacture a status with no
    # service-area identifier.
    has_service_area = bool(state.location.service_area_id or state.location.service_area_ids)
    if (
        state.location.match_status is not LocationMatchStatus.EXACT
        or not state.location.confirmed
        or not has_service_area
    ):
        missing.append(ScreeningField.LOCATION)
    if state.availability is None or not state.availability.value:
        missing.append(ScreeningField.AVAILABILITY)
    if state.preferred_schedule is None:
        missing.append(ScreeningField.PREFERRED_SCHEDULE)
    if state.delivery_experience is None:
        missing.append(ScreeningField.DELIVERY_EXPERIENCE)
    if state.start_availability is None:
        missing.append(ScreeningField.START_AVAILABILITY)
    if state.pending_confirmation is not None and state.pending_confirmation.field not in missing:
        missing.append(state.pending_confirmation.field)
    return missing


def evaluate_screening(
    state: ScreeningState,
    *,
    ruleset_version: str = RULESET_VERSION,
) -> ScreeningDecision:
    """Evaluate mandatory rules in a stable, documented order."""

    issues: list[ValidationIssue] = []
    missing = _missing_fields(state)
    trace: dict[str, Any] = {
        "drivers_license_present": state.drivers_license is not None,
        "drivers_license_required": True,
        "location_status": state.location.match_status.value,
        "location_confirmed": state.location.confirmed,
        "location_service_area_id": state.location.service_area_id,
        "location_service_area_ids": list(state.location.service_area_ids),
        "required_fields_complete": not missing,
        "candidate_confirmed": state.candidate_confirmed,
        "pending_confirmation": state.pending_confirmation is not None,
        "ruleset_version": ruleset_version,
    }

    if state.drivers_license is not None and state.drivers_license.value is False:
        return ScreeningDecision(
            status=ScreeningStatus.DISQUALIFIED,
            reason_codes=["no_drivers_license"],
            missing_fields=missing,
            ruleset_version=ruleset_version,
            rule_trace={**trace, "decision_rule": "drivers_license_required"},
        )

    if state.location.match_status is LocationMatchStatus.UNSUPPORTED:
        return ScreeningDecision(
            status=ScreeningStatus.DISQUALIFIED,
            reason_codes=["outside_service_area"],
            missing_fields=missing,
            ruleset_version=ruleset_version,
            rule_trace={**trace, "decision_rule": "service_area_membership"},
        )

    # Uncertain locations are still an active conversation concern.  The
    # controller records each clarification attempt and changes the decision
    # to ``needs_review`` only when the configured retry bound is exhausted.
    # A single fuzzy suggestion is represented by ``pending_confirmation`` and
    # follows the same in-progress path until the candidate answers it.
    if state.location.match_status in {
        LocationMatchStatus.AMBIGUOUS,
        LocationMatchStatus.NEEDS_CONFIRMATION,
    }:
        issues.append(
            ValidationIssue(
                field=ScreeningField.LOCATION,
                code="location_confirmation_required",
                message_key="location.confirmation_required",
            )
        )
        if state.pending_confirmation is None:
            return ScreeningDecision(
                status=ScreeningStatus.IN_PROGRESS,
                reason_codes=[ReviewReason.AMBIGUOUS_LOCATION.value],
                missing_fields=missing,
                issues=issues,
                ruleset_version=ruleset_version,
                rule_trace={**trace, "decision_rule": "location_uncertain"},
            )

    if state.pending_confirmation is not None:
        pending_reason = state.pending_confirmation.reason
        reason_codes = (
            [ReviewReason.AMBIGUOUS_LOCATION.value]
            if pending_reason == "service_area_city"
            else ["awaiting_confirmation"]
        )
        return ScreeningDecision(
            status=ScreeningStatus.IN_PROGRESS,
            reason_codes=reason_codes,
            missing_fields=missing,
            ruleset_version=ruleset_version,
            rule_trace={
                **trace,
                "decision_rule": (
                    "city_service_area_confirmation"
                    if pending_reason == "service_area_city"
                    else "pending_confirmation"
                ),
            },
        )

    if missing:
        return ScreeningDecision(
            status=ScreeningStatus.IN_PROGRESS,
            reason_codes=["required_information_missing"],
            missing_fields=missing,
            ruleset_version=ruleset_version,
            rule_trace={**trace, "decision_rule": "required_fields"},
        )

    if not state.candidate_confirmed:
        return ScreeningDecision(
            status=ScreeningStatus.IN_PROGRESS,
            reason_codes=["awaiting_candidate_confirmation"],
            ruleset_version=ruleset_version,
            rule_trace={**trace, "decision_rule": "final_confirmation"},
        )

    return ScreeningDecision(
        status=ScreeningStatus.QUALIFIED,
        reason_codes=["all_explicit_criteria_met"],
        ruleset_version=ruleset_version,
        rule_trace={**trace, "decision_rule": "all_explicit_criteria_met"},
    )


class ScreeningEngine:
    """Small façade around the pure evaluator for dependency injection."""

    def __init__(self, *, ruleset_version: str = RULESET_VERSION) -> None:
        self.ruleset_version = ruleset_version

    def evaluate(self, state: ScreeningState) -> ScreeningDecision:
        return evaluate_screening(state, ruleset_version=self.ruleset_version)

    def next_field(self, state: ScreeningState) -> ScreeningField | None:
        decision = self.evaluate(state)
        return decision.missing_fields[0] if decision.missing_fields else None
