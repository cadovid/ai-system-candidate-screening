"""Apply typed model interpretations to canonical state safely."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from candidate_screening.ai.schemas import (
    ExtractedDeliveryExperience,
    ExtractedLocation,
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
from candidate_screening.domain.service_areas import (
    ServiceAreaMatch,
    ServiceAreaMatcher,
    normalize_location,
)


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
        if (
            current.service_area_id
            or proposed.service_area_id
            or current.service_area_ids
            or proposed.service_area_ids
        ):
            return (
                current.service_area_id == proposed.service_area_id
                and current.service_area_ids == proposed.service_area_ids
                and current.match_status == proposed.match_status
            )
        return (
            current.city == proposed.city
            and current.normalized_value == proposed.normalized_value
            and current.match_status == proposed.match_status
        )
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
    if state.pending_confirmation is not None and state.pending_confirmation.field is not field:
        # ScreeningState intentionally stores one visible confirmation action.
        # Preserve that action rather than replacing it with a second
        # contradiction; fields without an existing value can still be
        # applied independently in the same turn.
        return False
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


def _location_raw_value(patch: ExtractedLocation) -> str:
    """Build a bounded raw location string from a typed extraction patch."""

    raw = patch.raw_value or " ".join(part for part in (patch.city, patch.zone) if part)
    return raw.strip()


def _repair_location_mislabelled_as_name(
    state: ScreeningState,
    interpretation: TurnInterpretation,
    *,
    service_area_matcher: ServiceAreaMatcher,
    issues: list[ValidationIssue],
) -> TurnInterpretation:
    """Keep a catalogue location from being persisted as a candidate name.

    A provider can occasionally assign a location phrase to the wrong
    structured field. A deterministic catalogue match gives us a safe,
    auditable signal for correcting that mapping. When the name question is
    active and no name exists, the value is rejected and the name is requested
    again; otherwise it is reinterpreted as a location so the candidate's
    known answer is not lost.
    """

    patch = interpretation.full_name
    if (
        patch is None
        or not patch.provided
        or not isinstance(patch.value, str)
        or not patch.value.strip()
    ):
        return interpretation
    if interpretation.location is not None and interpretation.location.provided:
        # A valid location patch is already present; discard only the
        # suspicious duplicate name field.
        match = service_area_matcher.match(patch.value)
        if match.area is None and match.city is None:
            return interpretation
        return interpretation.model_copy(update={"full_name": None})

    match = service_area_matcher.match(patch.value)
    if match.area is None and match.city is None:
        return interpretation
    if state.current_field is ScreeningField.FULL_NAME and state.full_name is None:
        issues.append(
            _issue(
                ScreeningField.FULL_NAME,
                "field_mismatch",
                "full_name.clarification_required",
            )
        )
        return interpretation.model_copy(update={"full_name": None})

    location = ExtractedLocation(
        raw_value=patch.value,
        city=match.city,
        zone=match.area.zone if match.area is not None else None,
        provided=True,
        correction=patch.correction,
        evidence=patch.evidence,
        confidence=patch.confidence,
    )
    return interpretation.model_copy(update={"full_name": None, "location": location})


def _repair_location_provided_flag(
    state: ScreeningState,
    interpretation: TurnInterpretation,
    *,
    service_area_matcher: ServiceAreaMatcher,
) -> TurnInterpretation:
    """Recover a location patch whose content contradicts ``provided=false``.

    ``provided`` is a model extraction hint, not a service-area decision. A
    provider can emit a valid location object with the flag left at its schema
    default, which would otherwise make reconciliation silently discard an
    explicit answer. The matcher must identify a catalogue result before the
    flag is changed. Unsupported model text is
    left absent, so it cannot become a disqualification or an eligible area by
    virtue of this recovery path. The coordinator separately gates recovery
    from an omitted nested patch on the active/pending location field because
    it has the original user message available for grounding.
    """

    patch = interpretation.location
    if patch is None or patch.provided or patch.ambiguous:
        return interpretation
    pending = state.pending_confirmation
    raw = _location_raw_value(patch)
    if not raw:
        return interpretation
    if not patch.evidence:
        # A patch with neither ``provided`` nor evidence is indistinguishable
        # from a provider default object. The coordinator can recover such a
        # patch from the original user message; direct reconciliation must not
        # promote model-only text into a candidate fact.
        return interpretation
    evidence = normalize_location(patch.evidence)
    raw_folded = normalize_location(raw)
    city = patch.city
    if city is None and pending is not None and pending.reason == "service_area_city":
        city = state.location.city
    match = service_area_matcher.match(raw, city=city, zone=patch.zone)
    # Evidence is required to be a quote from the current user message. If it
    # does not contain the structured phrase verbatim (for example, an
    # English ``city center`` quote paired with a Spanish ``Madrid Centro``
    # extraction), accept it only when deterministic matching proves that both
    # texts identify the same exact catalogue area or known city.
    if raw_folded not in evidence and not all(
        token in evidence.split() for token in raw_folded.split()
    ):
        evidence_match = service_area_matcher.match(
            patch.evidence,
        )
        same_area = (
            match.area is not None
            and evidence_match.area is not None
            and match.area.id == evidence_match.area.id
        )
        same_city = (
            match.is_city_level
            and evidence_match.is_city_level
            and match.city == evidence_match.city
        )
        if not (same_area or same_city):
            return interpretation
    if match.status not in {
        LocationMatchStatus.EXACT,
        LocationMatchStatus.AMBIGUOUS,
        LocationMatchStatus.NEEDS_CONFIRMATION,
    }:
        return interpretation
    repaired = patch.model_copy(
        update={
            "provided": True,
            "raw_value": raw,
            "city": patch.city or match.city or city,
            "confidence": max(patch.confidence, match.confidence),
        }
    )
    return interpretation.model_copy(update={"location": repaired})


def _location_from_match(
    patch: ExtractedLocation,
    *,
    raw: str,
    match: ServiceAreaMatch,
    message_id: str | None,
) -> LocationState:
    """Convert a matcher result into canonical, provenance-bearing state."""

    return LocationState(
        raw_value=raw,
        normalized_value=match.normalized_value,
        city=match.city,
        service_area_id=match.area.id if match.area else None,
        matched_name=match.area.display_name if match.area else match.city,
        match_status=match.status,
        suggestion_ids=match.suggestion_ids,
        confirmed=match.status is LocationMatchStatus.EXACT and match.area is not None,
        evidence=_evidence(patch.evidence, message_id=message_id),
    )


def _accepted_city_location(
    location: LocationState,
    *,
    area_names: list[str],
) -> LocationState:
    """Represent an explicit "any configured area" confirmation safely."""

    city = location.city or ""
    zones = ", ".join(area_names)
    matched_name = f"{city} — any configured area ({zones})" if zones else city
    return location.model_copy(
        update={
            "service_area_id": None,
            "service_area_ids": list(location.suggestion_ids),
            "matched_name": matched_name,
            "match_status": LocationMatchStatus.EXACT,
            "confirmed": True,
        }
    )


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
    trusted_fields: set[ScreeningField] | frozenset[ScreeningField] | None = None,
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
    # A model-extracted negative answer to a disqualifying criterion is kept
    # out of the canonical decision until the candidate confirms it.  The
    # coordinator passes an explicit (possibly empty) trust set for provider
    # interpretations; ``None`` preserves the lower-level reconciliation API's
    # legacy behavior for callers that already own a trusted interpretation.
    if (
        trusted_fields is not None
        and field is ScreeningField.DRIVERS_LICENSE
        and value is False
        and field not in trusted_fields
        and current is None
    ):
        state.pending_confirmation = PendingConfirmation(
            field=field,
            proposed_value=candidate.model_dump(mode="json"),
            prompt=prompt,
            reason="decision_impact_confirmation",
            created_at=now,
        )
        state.candidate_confirmed = False
        issues.append(
            _issue(field, "confirmation_required", f"{field.value}.confirmation_required")
        )
        return
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
    trusted_fields: set[ScreeningField] | frozenset[ScreeningField] | None = None,
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

    interpretation = _repair_location_provided_flag(
        next_state,
        interpretation,
        service_area_matcher=service_area_matcher,
    )
    interpretation = _repair_location_mislabelled_as_name(
        next_state,
        interpretation,
        service_area_matcher=service_area_matcher,
        issues=issues,
    )

    pending_unresolved = False
    pending_location_applied = False
    blocked_fields: set[ScreeningField] = set()

    # The browser language selector sends an explicit language-only
    # interpretation.  It is a control event, not an unanswered candidate
    # value: changing language while a city/zone confirmation is pending must
    # not consume a clarification attempt or trigger recruiter review.
    language_only = (
        interpretation.explicit_language is not None
        and interpretation.confirmation is None
        and interpretation.final_confirmation is None
        and interpretation.faq_complete is None
        and interpretation.full_name is None
        and interpretation.drivers_license is None
        and interpretation.location is None
        and interpretation.availability is None
        and interpretation.preferred_schedule is None
        and interpretation.delivery_experience is None
        and interpretation.start_availability is None
        and not interpretation.candidate_questions
    )
    if language_only:
        return ReconciliationResult(next_state)

    if next_state.pending_confirmation is not None:
        pending = next_state.pending_confirmation
        pending_field = pending.field
        if interpretation.confirmation is True:
            changed.extend(_apply_pending(next_state, timestamp))
            pending_location_applied = pending.field is ScreeningField.LOCATION
            blocked_fields.discard(pending_field)
        elif interpretation.confirmation is False:
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
            elif pending.field is ScreeningField.LOCATION and pending.reason == "service_area_city":
                # A recognized city plus a negative answer is a deterministic
                # outside-service-area result.  Keep the interpreted city and
                # offered zones for the rejection message, but do not retain
                # the pending city as an eligible location.
                next_state.location = next_state.location.model_copy(
                    update={
                        "service_area_id": None,
                        "service_area_ids": [],
                        "match_status": LocationMatchStatus.UNSUPPORTED,
                        "confirmed": False,
                    }
                )
                changed.append(ScreeningField.LOCATION)
            next_state.pending_confirmation = None
            next_state.candidate_confirmed = False
            pending_location_applied = pending.field is ScreeningField.LOCATION
            blocked_fields.discard(pending_field)
        elif (
            pending.field is ScreeningField.LOCATION
            and pending.reason == "service_area_city"
            and interpretation.location is not None
            and interpretation.location.provided
        ):
            # A candidate may answer the city offer with a concrete zone
            # instead of yes/no.  Accept it only when the exact zone is one of
            # the options previously derived from that city.
            location_patch = interpretation.location
            raw_location = _location_raw_value(location_patch)
            match = (
                service_area_matcher.match(
                    raw_location,
                    city=location_patch.city,
                    zone=location_patch.zone,
                )
                if raw_location
                and not location_patch.ambiguous
                and location_patch.confidence >= 0.55
                else None
            )
            proposed = pending.proposed_value
            proposed_mapping = cast(dict[str, Any], proposed) if isinstance(proposed, dict) else {}
            allowed_ids = {
                str(area_id)
                for area_id in proposed_mapping.get("service_area_ids", [])
                if isinstance(area_id, str)
            }
            if match is not None and match.status is LocationMatchStatus.EXACT and match.area:
                if match.area.id in allowed_ids:
                    next_state.location = _location_from_match(
                        location_patch,
                        raw=raw_location,
                        match=match,
                        message_id=message_id,
                    )
                    next_state.pending_confirmation = None
                    next_state.candidate_confirmed = False
                    next_state.current_field = ScreeningField.LOCATION
                    changed.append(ScreeningField.LOCATION)
                    pending_location_applied = True
                else:
                    pending_unresolved = True
            else:
                pending_unresolved = True
            if pending_unresolved:
                blocked_fields.add(pending_field)
                issues.append(
                    _issue(
                        ScreeningField.LOCATION,
                        "confirmation_required",
                        "location.city_confirmation_required",
                    )
                )
        else:
            pending_unresolved = True
            blocked_fields.add(pending_field)
            issues.append(
                _issue(
                    next_state.pending_confirmation.field,
                    "confirmation_required",
                    "confirmation.required",
                )
            )

    # A pending proposal blocks only its own field.  Independent fields in the
    # same message remain reconciliable; otherwise a correction to a name (or
    # a city offer) could silently discard a valid licence/availability answer.
    if ScreeningField.FULL_NAME not in blocked_fields and interpretation.full_name is not None:
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
            trusted_fields=trusted_fields,
        )
    if (
        ScreeningField.DRIVERS_LICENSE not in blocked_fields
        and interpretation.drivers_license is not None
    ):
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
            trusted_fields=trusted_fields,
        )
    if (
        ScreeningField.AVAILABILITY not in blocked_fields
        and interpretation.availability is not None
    ):
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
            trusted_fields=trusted_fields,
        )
    if (
        ScreeningField.PREFERRED_SCHEDULE not in blocked_fields
        and interpretation.preferred_schedule is not None
    ):
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
            trusted_fields=trusted_fields,
        )

    if (
        ScreeningField.LOCATION not in blocked_fields
        and not pending_location_applied
        and interpretation.location is not None
        and interpretation.location.provided
    ):
        patch_location = interpretation.location
        raw = _location_raw_value(patch_location)
        if not raw or patch_location.ambiguous or patch_location.confidence < 0.55:
            issues.append(
                _issue(
                    ScreeningField.LOCATION, "ambiguous_location", "location.clarification_required"
                )
            )
        else:
            match = service_area_matcher.match(
                raw,
                city=patch_location.city,
                zone=patch_location.zone,
            )
            location = _location_from_match(
                patch_location,
                raw=raw,
                match=match,
                message_id=message_id,
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
            elif match.is_city_level:
                # A known city without a concrete zone is not rejected and is
                # not silently mapped to the first area.  Ask the candidate
                # whether they can cover any configured zone in that city.
                area_names = [area.zone for area in match.suggestions]
                accepted_location = _accepted_city_location(
                    location,
                    area_names=area_names,
                )
                if (
                    next_state.location.match_status is LocationMatchStatus.EXACT
                    and next_state.location.confirmed
                ):
                    # Preserve an already trusted exact area when the
                    # candidate repeats only its city.  A different city is a
                    # correction and therefore requires the usual explicit
                    # replacement confirmation.
                    if next_state.location.city == location.city:
                        pass
                    elif not _set_or_request_confirmation(
                        next_state,
                        field=ScreeningField.LOCATION,
                        proposed_value=accepted_location,
                        current_value=next_state.location,
                        correction=patch_location.correction,
                        prompt="¿Confirmas esta ciudad? / Please confirm this city?",
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
                    next_state.pending_confirmation = PendingConfirmation(
                        field=ScreeningField.LOCATION,
                        proposed_value=accepted_location.model_dump(mode="json"),
                        prompt="",
                        reason="service_area_city",
                        created_at=timestamp,
                    )
                    changed.append(ScreeningField.LOCATION)
                    next_state.candidate_confirmed = False
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
            elif (
                trusted_fields is not None
                and match.status is LocationMatchStatus.UNSUPPORTED
                and ScreeningField.LOCATION not in trusted_fields
            ):
                # Unsupported location is a decision-impacting model claim.
                # Keep it as a candidate proposal until the user confirms it;
                # deterministic shortcuts may explicitly mark LOCATION as
                # trusted and retain the historical direct-reconciliation path.
                next_state.pending_confirmation = PendingConfirmation(
                    field=ScreeningField.LOCATION,
                    proposed_value=location.model_dump(mode="json"),
                    prompt="¿Confirmas esta zona? / Please confirm this service area.",
                    reason="decision_impact_confirmation",
                    created_at=timestamp,
                )
                next_state.candidate_confirmed = False
                issues.append(
                    _issue(
                        ScreeningField.LOCATION,
                        "confirmation_required",
                        "location.confirmation_required",
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
        ScreeningField.DELIVERY_EXPERIENCE not in blocked_fields
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
        ScreeningField.START_AVAILABILITY not in blocked_fields
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
    if next_state.faq_offer_made and interpretation.faq_complete is not None:
        next_state.faq_completed = interpretation.faq_complete
    if changed and next_state.faq_offer_made:
        # A correction reopens final review before the FAQ closing phase.
        next_state.faq_offer_made = False
        next_state.faq_completed = False
    next_state.current_field = None
    return ReconciliationResult(
        next_state, changed_fields=list(dict.fromkeys(changed)), issues=issues
    )
