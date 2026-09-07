"""Controlled conversational state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from candidate_screening.ai.schemas import TurnIntent, TurnInterpretation
from candidate_screening.domain.enums import (
    AvailabilityType,
    Language,
    SchedulePreference,
    ScreeningField,
    ScreeningStatus,
)
from candidate_screening.domain.models import (
    PendingConfirmation,
    ScreeningDecision,
    ScreeningState,
    ValidationIssue,
)
from candidate_screening.domain.rules import ScreeningEngine
from candidate_screening.domain.service_areas import ServiceAreaMatcher

from .conversation_copy import render_response_plan
from .faq import FAQCatalog
from .reconciliation import ReconciliationResult, reconcile_interpretation
from .response_plan import ResponseKind, ResponsePlan, VariantSelector, random_variant_index


@dataclass(slots=True)
class ConversationTurnResult:
    state: ScreeningState
    decision: ScreeningDecision
    assistant_message: str
    next_field: ScreeningField | None = None
    changed_fields: list[ScreeningField] = field(default_factory=lambda: list[ScreeningField]())
    security_event: bool = False
    opt_out: bool = False
    # Persisted as an operational counter only; the FAQ answer text itself is
    # already part of the assistant message and is never included in metrics.
    faq_answered: bool = False
    response_plan: ResponsePlan | None = None


class ConversationController:
    """Apply model interpretations, then choose a safe deterministic response."""

    def __init__(
        self,
        screening_engine: ScreeningEngine,
        service_area_matcher: ServiceAreaMatcher,
        *,
        max_clarifications: int = 2,
        faq_catalog: FAQCatalog | None = None,
        variant_selector: VariantSelector | None = None,
    ) -> None:
        if max_clarifications < 1:
            raise ValueError("max_clarifications must be at least one")
        self.screening_engine = screening_engine
        self.service_area_matcher = service_area_matcher
        self.max_clarifications = max_clarifications
        self.faq_catalog = faq_catalog
        # Copy varies between live turns, while tests can inject a deterministic
        # selector. The selected assistant message is persisted, so retries and
        # conversation recovery never redraw a different variant.
        self.variant_selector = variant_selector or random_variant_index

    def _response_plan(
        self,
        kind: ResponseKind,
        *,
        language: Language,
        state: ScreeningState,
        decision: ScreeningDecision | None = None,
        field: ScreeningField | None = None,
        pending: PendingConfirmation | None = None,
        faq_answer: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> ResponsePlan:
        context: dict[str, Any] = {}
        if extra_context:
            context.update(extra_context)
        if decision is not None and "outside_service_area" in decision.reason_codes:
            location = state.location
            context["raw_value"] = location.raw_value or location.matched_name
            context["city"] = location.city
            context["zones"] = [
                area.zone
                for area_id in location.suggestion_ids
                if (area := self.service_area_matcher.catalog.by_id(area_id)) is not None
            ]
        if pending is not None and pending.reason == "service_area_city":
            proposed = pending.proposed_value
            if isinstance(proposed, dict):
                proposed_mapping = cast(dict[str, Any], proposed)
                context["city"] = proposed_mapping.get("city")
                context["zones"] = [
                    area.zone
                    for area_id in proposed_mapping.get("service_area_ids", [])
                    if isinstance(area_id, str)
                    and (area := self.service_area_matcher.catalog.by_id(area_id)) is not None
                ]
        return ResponsePlan(
            kind=kind,
            language=language,
            field=field,
            pending=pending,
            decision=decision,
            state=state,
            faq_answer=faq_answer,
            variant_key=f"{kind.value}:{language.value}:{field.value if field else ''}",
            context=context,
        )

    @staticmethod
    def _understood_context(
        interpretation: TurnInterpretation,
        field: ScreeningField | None,
    ) -> dict[str, Any]:
        """Return safe, typed evidence useful for a candidate clarification.

        Only values already represented by the structured interpretation are
        rendered. Raw model prose and hidden reasoning never become response
        copy. Ambiguous values may still be described as "heard"; Python
        validation remains responsible for deciding whether they are accepted.
        """

        if field is None:
            return {}
        patch = getattr(interpretation, field.value, None)
        if patch is None or not getattr(patch, "provided", False):
            return {}

        understood: str | None = None
        if field is ScreeningField.FULL_NAME and isinstance(patch.value, str):
            understood = f"your name as {patch.value.strip()}"
        elif field is ScreeningField.DRIVERS_LICENSE and isinstance(patch.value, bool):
            understood = (
                "that you have a valid driver's licence"
                if patch.value
                else "that you do not have a valid driver's licence"
            )
        elif field is ScreeningField.AVAILABILITY and patch.value:
            labels = {
                AvailabilityType.FULL_TIME: "full-time availability",
                AvailabilityType.PART_TIME: "part-time availability",
                AvailabilityType.WEEKENDS: "weekend availability",
            }
            values = [labels.get(value, str(value)) for value in patch.value]
            understood = " or ".join(values)
        elif field is ScreeningField.PREFERRED_SCHEDULE and isinstance(
            patch.value, SchedulePreference
        ):
            understood = f"a {patch.value.value.replace('_', ' ')} schedule"
        elif field is ScreeningField.LOCATION:
            location_value = patch.raw_value or " ".join(
                value for value in (patch.city, patch.zone) if value
            )
            if location_value:
                understood = f"the location {location_value.strip()}"
        elif field is ScreeningField.DELIVERY_EXPERIENCE:
            parts: list[str] = []
            if patch.years is not None:
                years = int(patch.years) if patch.years.is_integer() else patch.years
                parts.append(f"{years} year{'s' if years != 1 else ''} of delivery experience")
            if patch.platforms:
                parts.append("experience with " + ", ".join(patch.platforms[:3]))
            if parts:
                understood = " and ".join(parts)
        elif field is ScreeningField.START_AVAILABILITY and patch.raw_value:
            understood = f"that you could start {patch.raw_value.strip()}"

        return {"understood": understood} if understood else {}

    def _render(self, plan: ResponsePlan) -> str:
        return render_response_plan(plan, selector=self.variant_selector)

    @staticmethod
    def introduction(language: Language = Language.ES) -> str:
        if language is Language.EN:
            return (
                "Hi! I’m the automated AI screening assistant for the delivery-driver role. "
                "I’ll collect a few job-related details for a recruiter. You can stop at any time. "
                "Please don’t share unrelated sensitive information. Would you like to continue?"
            )
        return (
            "¡Hola! Soy el asistente automatizado de IA para el puesto de repartidor/a. "
            "Recogeré algunos datos relacionados con el trabajo para una persona reclutadora. "
            "Puedes parar cuando quieras. No compartas información sensible que no sea necesaria. "
            "¿Quieres continuar?"
        )

    def process(
        self,
        state: ScreeningState,
        interpretation: TurnInterpretation,
        *,
        faq_answer: str | None = None,
        now: datetime | None = None,
        message_id: str | None = None,
        trusted_fields: set[ScreeningField] | frozenset[ScreeningField] | None = None,
    ) -> ConversationTurnResult:
        timestamp = now or datetime.now(UTC)
        reconciliation: ReconciliationResult = reconcile_interpretation(
            state,
            interpretation,
            service_area_matcher=self.service_area_matcher,
            now=timestamp,
            message_id=message_id,
            trusted_fields=trusted_fields,
        )
        next_state = reconciliation.state
        language = next_state.preferred_language
        if faq_answer is None and self.faq_catalog is not None:
            for question in interpretation.candidate_questions:
                faq_answer = self.faq_catalog.answer(question, language)
                if faq_answer:
                    break

        if reconciliation.opt_out:
            decision = ScreeningDecision(
                status=ScreeningStatus.ABANDONED,
                reason_codes=["candidate_opted_out"],
                ruleset_version=next_state.ruleset_version,
            )
            plan = self._response_plan(
                ResponseKind.OPT_OUT,
                language=language,
                state=next_state,
                decision=decision,
            )
            return ConversationTurnResult(
                next_state,
                decision,
                self._render(plan),
                changed_fields=reconciliation.changed_fields,
                security_event=reconciliation.security_event,
                opt_out=True,
                response_plan=plan,
            )

        if reconciliation.security_event:
            decision = self.screening_engine.evaluate(next_state)
            kind = (
                ResponseKind.SENSITIVE
                if interpretation.sensitive_data_detected
                else ResponseKind.SECURITY
            )
            next_field = next_state.current_field or self.screening_engine.next_field(next_state)
            plan = self._response_plan(
                kind,
                language=language,
                state=next_state,
                decision=decision,
                field=next_field,
            )
            return ConversationTurnResult(
                next_state,
                decision,
                self._render(plan),
                next_field=next_field,
                changed_fields=reconciliation.changed_fields,
                security_event=True,
                response_plan=plan,
            )

        decision = self.screening_engine.evaluate(next_state)

        if decision.status is ScreeningStatus.QUALIFIED and not next_state.faq_completed:
            workflow_decision = decision.model_copy(
                update={
                    "status": ScreeningStatus.IN_PROGRESS,
                    "reason_codes": ["awaiting_candidate_questions"],
                    "rule_trace": {
                        **decision.rule_trace,
                        "conversation_rule": "post_screening_faq",
                        "eligibility_status": ScreeningStatus.QUALIFIED.value,
                    },
                }
            )
            offer_was_already_made = next_state.faq_offer_made
            next_state.faq_offer_made = True
            next_state.current_field = None
            has_question = bool(interpretation.candidate_questions)
            plan = self._response_plan(
                (
                    ResponseKind.FAQ_FOLLOWUP
                    if offer_was_already_made or has_question
                    else ResponseKind.FAQ_OFFER
                ),
                language=language,
                state=next_state,
                decision=workflow_decision,
                faq_answer=faq_answer,
                extra_context={"has_question": has_question},
            )
            return ConversationTurnResult(
                next_state,
                workflow_decision,
                self._render(plan),
                next_field=None,
                changed_fields=reconciliation.changed_fields,
                faq_answered=bool(faq_answer),
                response_plan=plan,
            )

        response_issues = list(reconciliation.issues)
        countable_issues = list(response_issues)
        # A question/off-topic intent is a detour only when the model supplied
        # an actual candidate question or explicitly marked the turn off-topic.
        # A provider can otherwise label an answer such as "Flexible, depends
        # on the company" as a question while omitting the field patch; that
        # must become a field clarification, not the same prompt again.
        conversational_control = interpretation.intent is TurnIntent.OFF_TOPIC or (
            interpretation.intent in {TurnIntent.QUESTION, TurnIntent.MIXED}
            and bool(interpretation.candidate_questions)
        )
        if conversational_control:
            # Questions/social detours are answered or bounded and then bridge
            # back to the active field; they are not failed candidate answers.
            countable_issues = []

        # A valid-shaped answer that changes no canonical value is itself a
        # field-specific clarification opportunity.  Avoid doing this for
        # language selectors, FAQ/social turns, explicit confirmation events,
        # and pending city/correction prompts that already have an issue.
        no_effect_field = None
        has_candidate_patch = any(
            patch is not None
            for patch in (
                interpretation.full_name,
                interpretation.drivers_license,
                interpretation.location,
                interpretation.availability,
                interpretation.preferred_schedule,
                interpretation.delivery_experience,
                interpretation.start_availability,
            )
        )
        language_only = (
            interpretation.explicit_language is not None
            and not has_candidate_patch
            and interpretation.confirmation is None
            and interpretation.final_confirmation is None
            and not interpretation.candidate_questions
        )
        if (
            not response_issues
            and decision.status is ScreeningStatus.IN_PROGRESS
            and not reconciliation.changed_fields
            and not next_state.pending_confirmation
            and not conversational_control
            and not language_only
            and interpretation.disclosure_acknowledged is not True
            and interpretation.confirmation is None
            and interpretation.final_confirmation is None
            and (
                interpretation.intent
                in {TurnIntent.ANSWER, TurnIntent.CORRECTION, TurnIntent.UNKNOWN}
                or (
                    interpretation.intent in {TurnIntent.QUESTION, TurnIntent.MIXED}
                    and not interpretation.candidate_questions
                )
            )
        ):
            no_effect_field = next_state.current_field or self.screening_engine.next_field(
                next_state
            )
            if no_effect_field is not None:
                response_issues.append(
                    ValidationIssue(
                        field=no_effect_field,
                        code="no_effect_answer",
                        message_key=f"{no_effect_field.value}.clarification_required",
                    )
                )
                countable_issues = list(response_issues)

        # Clarification attempts are state, not model judgment.  Once the
        # configured bound is exhausted, hand the case to a recruiter while
        # retaining the last trusted canonical values.
        if countable_issues:
            issue_fields = {issue.field for issue in countable_issues if issue.field is not None}
            for issue_field in issue_fields:
                count = next_state.clarification_counts.get(issue_field, 0) + 1
                next_state.clarification_counts[issue_field] = count
            exhausted = any(
                next_state.clarification_counts.get(issue_field, 0) >= self.max_clarifications
                for issue_field in issue_fields
            )
            if exhausted:
                decision = ScreeningDecision(
                    status=ScreeningStatus.NEEDS_REVIEW,
                    reason_codes=["retry_limit"],
                    missing_fields=decision.missing_fields,
                    issues=response_issues,
                    ruleset_version=next_state.ruleset_version,
                    rule_trace={
                        **decision.rule_trace,
                        "decision_rule": "clarification_retry_limit",
                        "clarification_counts": {
                            field.value: count
                            for field, count in next_state.clarification_counts.items()
                        },
                    },
                )
        for changed_field in reconciliation.changed_fields:
            # Resolution starts a fresh clarification budget for that field.
            next_state.clarification_counts[changed_field] = 0
        if decision.status is ScreeningStatus.DISQUALIFIED:
            plan = self._response_plan(
                ResponseKind.DISQUALIFIED,
                language=language,
                state=next_state,
                decision=decision,
            )
            return ConversationTurnResult(
                next_state,
                decision,
                self._render(plan),
                changed_fields=reconciliation.changed_fields,
                response_plan=plan,
            )
        if decision.status is ScreeningStatus.QUALIFIED:
            plan = self._response_plan(
                ResponseKind.QUALIFIED,
                language=language,
                state=next_state,
                decision=decision,
            )
            return ConversationTurnResult(
                next_state,
                decision,
                self._render(plan),
                changed_fields=reconciliation.changed_fields,
                response_plan=plan,
            )
        if "retry_limit" in decision.reason_codes:
            review_field = next(
                (issue.field for issue in response_issues if issue.field is not None),
                next_state.current_field,
            )
            plan = self._response_plan(
                ResponseKind.NEEDS_REVIEW,
                language=language,
                state=next_state,
                decision=decision,
                field=review_field,
                extra_context=self._understood_context(interpretation, review_field),
            )
            return ConversationTurnResult(
                next_state,
                decision,
                self._render(plan),
                changed_fields=reconciliation.changed_fields,
                response_plan=plan,
            )

        if response_issues:
            field = response_issues[0].field or self.screening_engine.next_field(next_state)
            if field is not None:
                next_state.current_field = field
            kind = (
                ResponseKind.PENDING_CONFIRMATION
                if next_state.pending_confirmation is not None
                else ResponseKind.CLARIFICATION
            )
        elif next_state.pending_confirmation is not None:
            next_state.current_field = next_state.pending_confirmation.field
            field = next_state.current_field
            kind = ResponseKind.PENDING_CONFIRMATION
        else:
            field = self.screening_engine.next_field(next_state)
            next_state.current_field = field
            kind = ResponseKind.FINAL_CONFIRMATION if field is None else ResponseKind.PROMPT

        if faq_answer:
            if kind not in {ResponseKind.PENDING_CONFIRMATION, ResponseKind.CLARIFICATION}:
                kind = ResponseKind.FAQ_BRIDGE
        elif (
            interpretation.intent in {TurnIntent.QUESTION, TurnIntent.MIXED, TurnIntent.OFF_TOPIC}
            and interpretation.response_requested
            and conversational_control
        ):
            kind = ResponseKind.UNKNOWN_QUESTION
        plan = self._response_plan(
            kind,
            language=language,
            state=next_state,
            decision=decision,
            field=field,
            pending=next_state.pending_confirmation,
            faq_answer=faq_answer,
            extra_context=(
                self._understood_context(interpretation, field)
                if kind is ResponseKind.CLARIFICATION
                else None
            ),
        )

        return ConversationTurnResult(
            next_state,
            decision,
            self._render(plan),
            next_field=next_state.current_field,
            changed_fields=reconciliation.changed_fields,
            security_event=reconciliation.security_event,
            faq_answered=bool(faq_answer),
            response_plan=plan,
        )
