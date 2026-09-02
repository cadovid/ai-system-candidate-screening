"""Apply typed model interpretations to canonical state safely."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from candidate_screening.ai.schemas import (
    ExtractedDeliveryExperience,
    ExtractedValue,
    StartAvailabilityExtraction,
    TurnIntent,
    TurnInterpretation,
)
from candidate_screening.domain.enums import (
    AvailabilityType,
    LocationMatchStatus,
    SchedulePreference,
    ScreeningField,
)
from candidate_screening.domain.models import (
    DeliveryExperience,
    Evidence,
    LocationState,
    PendingConfirmation,
    ScreeningState,
    SourcedValue,
    StartAvailability,
    StartDatePrecision,
    ValidationIssue,
)
from candidate_screening.domain.service_areas import ServiceAreaMatcher


@dataclass(slots=True)
class ReconciliationResult:
    state: ScreeningState
    changed_fields: list[ScreeningField] = field(default_factory=lambda: list[ScreeningField]())
    issues: list[ValidationIssue] = field(default_factory=lambda: list[ValidationIssue]())
    security_event: bool = False
    opt_out: bool = False


def _evidence(text: str, *, message_id: str | None = None) -> Evidence:
    return Evidence(message_id=message_id, quote=text[:500])


def _issue(field: ScreeningField, code: str, key: str) -> ValidationIssue:
    return ValidationIssue(field=field, code=code, message_key=key)


def _same_value(current: Any, proposed: Any) -> bool:
    """Compare factual values while ignoring provenance metadata.

    A repeated answer should not become a correction merely because it was
    captured in a different message (and therefore has different evidence or
    timestamps).
    """

    if current is None or proposed is None:
        return current is proposed
    if isinstance(current, SourcedValue):
        current = cast(SourcedValue[Any], current).value
    if isinstance(proposed, SourcedValue):
        proposed = cast(SourcedValue[Any], proposed).value
    if isinstance(current, LocationState) and isinstance(proposed, LocationState):
        if current.service_area_id or proposed.service_area_id:
            return (
                current.service_area_id == proposed.service_area_id
                and current.match_status == proposed.match_status
            )
        return current.normalized_value == proposed.normalized_value
    if isinstance(current, DeliveryExperience) and isinstance(proposed, DeliveryExperience):
        return current.years == proposed.years and [
            item.casefold() for item in current.platforms
        ] == [item.casefold() for item in proposed.platforms]
    if isinstance(current, StartAvailability) and isinstance(proposed, StartAvailability):
        return (
            current.date == proposed.date
            and current.precision == proposed.precision
            and current.raw_value.casefold() == proposed.raw_value.casefold()
        )
    return current == proposed


def _set_or_request_confirmation(
    state: ScreeningState,
    *,
    field: ScreeningField,
    proposed_value: Any,
    current_value: Any,
    correction: bool,
    prompt: str,
    now: datetime,
) -> bool:
    """Apply a new value or preserve the old one pending explicit correction."""

    if current_value is None or _same_value(current_value, proposed_value):
        return True
    state.pending_confirmation = PendingConfirmation(
        field=field,
        proposed_value=proposed_value.model_dump(mode="json")
        if hasattr(proposed_value, "model_dump")
        else proposed_value,
        prompt=prompt,
        reason="candidate_correction" if correction else "contradictory_answer",
        created_at=now,
    )
    state.candidate_confirmed = False
    return False


def _apply_pending(state: ScreeningState, now: datetime) -> list[ScreeningField]:
    pending = state.pending_confirmation
    if pending is None:
        return []
    field = pending.field
    value = pending.proposed_value
    changed: list[ScreeningField] = []
    if field is ScreeningField.FULL_NAME:
        state.full_name = SourcedValue[str].model_validate(value)
    elif field is ScreeningField.DRIVERS_LICENSE:
        state.drivers_license = SourcedValue[bool].model_validate(value)
    elif field is ScreeningField.LOCATION:
        state.location = LocationState.model_validate(value)
    elif field is ScreeningField.AVAILABILITY:
        state.availability = SourcedValue[list[AvailabilityType]].model_validate(value)
    elif field is ScreeningField.PREFERRED_SCHEDULE:
        state.preferred_schedule = SourcedValue[SchedulePreference].model_validate(value)
    elif field is ScreeningField.DELIVERY_EXPERIENCE:
        state.delivery_experience = DeliveryExperience.model_validate(value)
    elif field is ScreeningField.START_AVAILABILITY:
        state.start_availability = StartAvailability.model_validate(value)
    changed.append(field)
    state.pending_confirmation = None
    state.candidate_confirmed = False
    state.current_field = field
    _ = now
    return changed


def _apply_scalar_patch(
    state: ScreeningState,
    *,
    field: ScreeningField,
    patch: ExtractedValue[Any],
    current: Any,
    value: Any,
    prompt: str,
    changed: list[ScreeningField],
    issues: list[ValidationIssue],
    now: datetime,
    message_id: str | None,
) -> None:
    if not patch.provided:
        return
    if patch.ambiguous or patch.value is None or patch.confidence < 0.55:
        issues.append(_issue(field, "ambiguous_value", f"{field.value}.clarification_required"))
        return
    candidate = SourcedValue(
        value=value,
        evidence=_evidence(patch.evidence, message_id=message_id),
        confidence=patch.confidence,
        captured_at=now,
    )
    if not _set_or_request_confirmation(
        state,
        field=field,
        proposed_value=candidate,
        current_value=current,
        correction=patch.correction,
        prompt=prompt,
        now=now,
    ):
        issues.append(
            _issue(field, "confirmation_required", f"{field.value}.confirmation_required")
        )
        return
    if field is ScreeningField.FULL_NAME:
        state.full_name = candidate
    elif field is ScreeningField.DRIVERS_LICENSE:
        state.drivers_license = candidate
    elif field is ScreeningField.AVAILABILITY:
        state.availability = candidate
    elif field is ScreeningField.PREFERRED_SCHEDULE:
        state.preferred_schedule = candidate
    changed.append(field)
    state.candidate_confirmed = False


def reconcile_interpretation(
    state: ScreeningState,
    interpretation: TurnInterpretation,
    *,
    service_area_matcher: ServiceAreaMatcher,
    now: datetime | None = None,
    message_id: str | None = None,
) -> ReconciliationResult:
    """Reconcile one structured interpretation without trusting its status.

    Existing values are not silently overwritten. A changed value is placed in
    ``pending_confirmation`` and only applied after a positive confirmation.
    """

    timestamp = now or datetime.now(UTC)
    next_state = state.model_copy(deep=True)
    changed: list[ScreeningField] = []
    issues: list[ValidationIssue] = []
    security_event = (
        interpretation.prompt_injection_detected
        or interpretation.sensitive_data_detected
        or interpretation.intent is TurnIntent.PROMPT_INJECTION
    )
    if security_event:
        # Prompt-injection content is untrusted and must not even switch the
        # canonical language or alter a pending correction.  The controller can
        # still answer safely using the last trusted state.
        return ReconciliationResult(next_state, security_event=True)
    if interpretation.explicit_language is not None:
        next_state.preferred_language = interpretation.explicit_language
    elif interpretation.language_confidence >= 0.8:
        next_state.preferred_language = interpretation.detected_language

    if interpretation.opt_out_requested or interpretation.intent is TurnIntent.OPT_OUT:
        return ReconciliationResult(next_state, security_event=security_event, opt_out=True)

    if interpretation.disclosure_acknowledged is True:
        next_state.disclosure_acknowledged = True

    pending_unresolved = False

    if next_state.pending_confirmation is not None:
        if interpretation.confirmation is True:
            changed.extend(_apply_pending(next_state, timestamp))
        elif interpretation.confirmation is False:
            pending = next_state.pending_confirmation
            # A one-item fuzzy suggestion keeps the unresolved match and
            # evidence in canonical state while awaiting confirmation.  On a
            # negative answer there is no trusted location to retain, so
            # reset it; correction proposals over a trusted exact value are
            # left untouched.
            if (
                pending.field is ScreeningField.LOCATION
                and pending.reason == "service_area_suggestion"
                and next_state.location.match_status is LocationMatchStatus.NEEDS_CONFIRMATION
                and next_state.location.service_area_id is None
            ):
                next_state.location = LocationState()
            next_state.pending_confirmation = None
            next_state.candidate_confirmed = False
        else:
            pending_unresolved = True
            issues.append(
                _issue(
                    next_state.pending_confirmation.field,
                    "confirmation_required",
                    "confirmation.required",
                )
            )

    # Do not allow a model to stack another correction on a pending one.  The
    # candidate must resolve the trusted proposal first.
    if not pending_unresolved and interpretation.full_name is not None:
        patch = interpretation.full_name
        _apply_scalar_patch(
            next_state,
            field=ScreeningField.FULL_NAME,
            patch=patch,
            current=next_state.full_name,
            value=patch.value,
            prompt="¿Quieres cambiar tu nombre? / Would you like to change your name?",
            changed=changed,
            issues=issues,
            now=timestamp,
            message_id=message_id,
        )
    if not pending_unresolved and interpretation.drivers_license is not None:
        patch = interpretation.drivers_license
        _apply_scalar_patch(
            next_state,
            field=ScreeningField.DRIVERS_LICENSE,
            patch=patch,
            current=next_state.drivers_license,
            value=patch.value,
            prompt="¿Confirmas esta respuesta sobre tu licencia? / Please confirm your licence answer.",
            changed=changed,
            issues=issues,
            now=timestamp,
            message_id=message_id,
        )
    if not pending_unresolved and interpretation.availability is not None:
        patch = interpretation.availability
        _apply_scalar_patch(
            next_state,
            field=ScreeningField.AVAILABILITY,
            patch=patch,
            current=next_state.availability,
            value=patch.value,
            prompt="¿Quieres cambiar tu disponibilidad? / Would you like to change your availability?",
            changed=changed,
            issues=issues,
            now=timestamp,
            message_id=message_id,
        )
    if not pending_unresolved and interpretation.preferred_schedule is not None:
        patch = interpretation.preferred_schedule
        _apply_scalar_patch(
            next_state,
            field=ScreeningField.PREFERRED_SCHEDULE,
            patch=patch,
            current=next_state.preferred_schedule,
            value=patch.value,
            prompt="¿Quieres cambiar tu horario? / Would you like to change your schedule?",
            changed=changed,
            issues=issues,
            now=timestamp,
            message_id=message_id,
        )

    if (
        not pending_unresolved
        and interpretation.location is not None
        and interpretation.location.provided
    ):
        patch_location = interpretation.location
        raw = patch_location.raw_value or " ".join(
            part for part in (patch_location.city, patch_location.zone) if part
        )
        if not raw or patch_location.ambiguous or patch_location.confidence < 0.55:
            issues.append(
                _issue(
                    ScreeningField.LOCATION, "ambiguous_location", "location.clarification_required"
                )
            )
        else:
            match = service_area_matcher.match(raw)
            location = LocationState(
                raw_value=raw,
                normalized_value=match.normalized_value,
                service_area_id=match.area.id if match.area else None,
                matched_name=match.area.display_name if match.area else None,
                match_status=match.status,
                suggestion_ids=match.suggestion_ids,
                confirmed=match.status is LocationMatchStatus.EXACT,
                evidence=_evidence(patch_location.evidence, message_id=message_id),
            )
            if (
                match.status is LocationMatchStatus.NEEDS_CONFIRMATION
                and len(match.suggestions) == 1
            ):
                suggested = match.suggestions[0]
                if patch_location.explicit_confirmation:
                    # The model may identify an explicit "yes" to the one
                    # deterministic suggestion in the same message.
                    location = location.model_copy(
                        update={
                            "service_area_id": suggested.id,
                            "matched_name": suggested.display_name,
                            "match_status": LocationMatchStatus.EXACT,
                            "confirmed": True,
                        }
                    )
                    proposed_location = location
                else:
                    proposed_location = location.model_copy(
                        update={
                            "service_area_id": suggested.id,
                            "matched_name": suggested.display_name,
                            "match_status": LocationMatchStatus.EXACT,
                            "confirmed": True,
                        }
                    )
                has_current = bool(next_state.location.raw_value)
                if has_current and not _same_value(next_state.location, location):
                    applied = _set_or_request_confirmation(
                        next_state,
                        field=ScreeningField.LOCATION,
                        proposed_value=proposed_location,
                        current_value=next_state.location,
                        correction=patch_location.correction,
                        prompt="¿Confirmas esta zona? / Please confirm this service area.",
                        now=timestamp,
                    )
                else:
                    if patch_location.explicit_confirmation:
                        next_state.location = location
                        applied = True
                    else:
                        next_state.location = location
                        applied = True
                        next_state.pending_confirmation = PendingConfirmation(
                            field=ScreeningField.LOCATION,
                            proposed_value=proposed_location.model_dump(mode="json"),
                            prompt=(
                                f"¿Te refieres a {suggested.display_name}? / "
                                f"Do you mean {suggested.display_name}?"
                            ),
                            reason="service_area_suggestion",
                            created_at=timestamp,
                        )
                if applied:
                    changed.append(ScreeningField.LOCATION)
                    next_state.candidate_confirmed = False
                if (
                    not patch_location.explicit_confirmation
                    and next_state.pending_confirmation is None
                ):
                    issues.append(
                        _issue(
                            ScreeningField.LOCATION,
                            "confirmation_required",
                            "location.confirmation_required",
                        )
                    )
            elif match.status is LocationMatchStatus.AMBIGUOUS:
                # Keep the evidence and suggestions, but never select one of
                # several areas automatically.  If a trusted location already
                # exists, retain it until the candidate supplies an explicit
                # correction; otherwise preserve the ambiguous evidence.
                if not next_state.location.raw_value:
                    next_state.location = location
                    changed.append(ScreeningField.LOCATION)
                    next_state.candidate_confirmed = False
                issues.append(
                    _issue(
                        ScreeningField.LOCATION,
                        "ambiguous_location",
                        "location.clarification_required",
                    )
                )
            elif not _set_or_request_confirmation(
                next_state,
                field=ScreeningField.LOCATION,
                proposed_value=location,
                # An ambiguous/uncertain location is not a trusted value, so
                # an exact clarification can resolve it directly.  A
                # previously confirmed exact area still requires explicit
                # confirmation before a correction replaces it.
                current_value=(
                    next_state.location
                    if (
                        next_state.location.match_status is LocationMatchStatus.EXACT
                        and next_state.location.confirmed
                        and next_state.location.service_area_id
                    )
                    else None
                ),
                correction=patch_location.correction,
                prompt="¿Confirmas esta zona? / Please confirm this service area.",
                now=timestamp,
            ):
                issues.append(
                    _issue(
                        ScreeningField.LOCATION,
                        "confirmation_required",
                        "location.confirmation_required",
                    )
                )
            else:
                next_state.location = location
                changed.append(ScreeningField.LOCATION)
                next_state.candidate_confirmed = False

    if (
        not pending_unresolved
        and interpretation.delivery_experience is not None
        and interpretation.delivery_experience.provided
    ):
        patch_experience: ExtractedDeliveryExperience = interpretation.delivery_experience
        if (
            patch_experience.years is None
            or patch_experience.ambiguous
            or patch_experience.confidence < 0.55
        ):
            issues.append(
                _issue(
                    ScreeningField.DELIVERY_EXPERIENCE,
                    "ambiguous_experience",
                    "delivery_experience.clarification_required",
                )
            )
        else:
            experience = DeliveryExperience(
                years=patch_experience.years,
                platforms=patch_experience.platforms,
                evidence=_evidence(patch_experience.evidence, message_id=message_id),
            )
            if _set_or_request_confirmation(
                next_state,
                field=ScreeningField.DELIVERY_EXPERIENCE,
                proposed_value=experience,
                current_value=next_state.delivery_experience,
                correction=patch_experience.correction,
                prompt="¿Quieres cambiar tu experiencia? / Would you like to change your experience?",
                now=timestamp,
            ):
                next_state.delivery_experience = experience
                changed.append(ScreeningField.DELIVERY_EXPERIENCE)
                next_state.candidate_confirmed = False
            else:
                issues.append(
                    _issue(
                        ScreeningField.DELIVERY_EXPERIENCE,
                        "confirmation_required",
                        "delivery_experience.confirmation_required",
                    )
                )

    if (
        not pending_unresolved
        and interpretation.start_availability is not None
        and interpretation.start_availability.provided
    ):
        patch_start: StartAvailabilityExtraction = interpretation.start_availability
        if not patch_start.raw_value or patch_start.ambiguous or patch_start.confidence < 0.55:
            issues.append(
                _issue(
                    ScreeningField.START_AVAILABILITY,
                    "ambiguous_start_date",
                    "start_availability.clarification_required",
                )
            )
        else:
            try:
                precision = StartDatePrecision(patch_start.precision)
            except ValueError:
                precision = StartDatePrecision.UNKNOWN
            start = StartAvailability(
                raw_value=patch_start.raw_value,
                date=patch_start.date,
                precision=precision,
                timezone=patch_start.timezone,
                evidence=_evidence(patch_start.evidence, message_id=message_id),
            )
            if _set_or_request_confirmation(
                next_state,
                field=ScreeningField.START_AVAILABILITY,
                proposed_value=start,
                current_value=next_state.start_availability,
                correction=patch_start.correction,
                prompt="¿Quieres cambiar tu fecha de inicio? / Would you like to change your start date?",
                now=timestamp,
            ):
                next_state.start_availability = start
                changed.append(ScreeningField.START_AVAILABILITY)
                next_state.candidate_confirmed = False
            else:
                issues.append(
                    _issue(
                        ScreeningField.START_AVAILABILITY,
                        "confirmation_required",
                        "start_availability.confirmation_required",
                    )
                )

    if interpretation.final_confirmation is not None:
        next_state.candidate_confirmed = interpretation.final_confirmation
    next_state.current_field = None
    return ReconciliationResult(
        next_state, changed_fields=list(dict.fromkeys(changed)), issues=issues
    )
