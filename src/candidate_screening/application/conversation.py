"""Controlled conversational state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from candidate_screening.ai.schemas import TurnInterpretation
from candidate_screening.domain.enums import Language, ScreeningField, ScreeningStatus
from candidate_screening.domain.models import PendingConfirmation, ScreeningDecision, ScreeningState
from candidate_screening.domain.rules import ScreeningEngine
from candidate_screening.domain.service_areas import ServiceAreaMatcher

from .faq import FAQCatalog
from .reconciliation import ReconciliationResult, reconcile_interpretation


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


_PROMPTS: dict[Language, dict[ScreeningField, str]] = {
    Language.ES: {
        ScreeningField.FULL_NAME: "¿Cuál es tu nombre completo?",
        ScreeningField.DRIVERS_LICENSE: "¿Tienes una licencia de conducir vigente? Responde sí o no.",
        ScreeningField.LOCATION: "¿En qué ciudad y zona te gustaría repartir?",
        ScreeningField.AVAILABILITY: "¿Buscas trabajar a tiempo completo, a tiempo parcial o los fines de semana?",
        ScreeningField.PREFERRED_SCHEDULE: "¿Qué horario prefieres: mañana, tarde, noche o flexible?",
        ScreeningField.DELIVERY_EXPERIENCE: "¿Cuántos años de experiencia en reparto tienes? Si quieres, indica también las plataformas.",
        ScreeningField.START_AVAILABILITY: "¿Cuándo podrías empezar?",
    },
    Language.EN: {
        ScreeningField.FULL_NAME: "What is your full name?",
        ScreeningField.DRIVERS_LICENSE: "Do you have a valid driver's licence? Please answer yes or no.",
        ScreeningField.LOCATION: "Which city and area would you like to deliver in?",
        ScreeningField.AVAILABILITY: "Are you looking for full-time, part-time, or weekend work?",
        ScreeningField.PREFERRED_SCHEDULE: "Which schedule do you prefer: morning, afternoon, evening, or flexible?",
        ScreeningField.DELIVERY_EXPERIENCE: "How many years of delivery experience do you have? You can also name platforms.",
        ScreeningField.START_AVAILABILITY: "When could you start?",
    },
}


class ConversationController:
    """Apply model interpretations, then choose a safe deterministic response."""

    def __init__(
        self,
        screening_engine: ScreeningEngine,
        service_area_matcher: ServiceAreaMatcher,
        *,
        max_clarifications: int = 2,
        faq_catalog: FAQCatalog | None = None,
    ) -> None:
        if max_clarifications < 1:
            raise ValueError("max_clarifications must be at least one")
        self.screening_engine = screening_engine
        self.service_area_matcher = service_area_matcher
        self.max_clarifications = max_clarifications
        self.faq_catalog = faq_catalog

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
    ) -> ConversationTurnResult:
        timestamp = now or datetime.now(UTC)
        reconciliation: ReconciliationResult = reconcile_interpretation(
            state,
            interpretation,
            service_area_matcher=self.service_area_matcher,
            now=timestamp,
            message_id=message_id,
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
            return ConversationTurnResult(
                next_state,
                decision,
                self._opt_out_message(language),
                changed_fields=reconciliation.changed_fields,
                security_event=reconciliation.security_event,
                opt_out=True,
            )

        if reconciliation.security_event:
            decision = self.screening_engine.evaluate(next_state)
            return ConversationTurnResult(
                next_state,
                decision,
                (
                    self._sensitive_message(language, next_state)
                    if interpretation.sensitive_data_detected
                    else self._security_message(language, next_state)
                ),
                next_field=next_state.current_field or self.screening_engine.next_field(next_state),
                changed_fields=reconciliation.changed_fields,
                security_event=True,
            )

        decision = self.screening_engine.evaluate(next_state)
        # Clarification attempts are state, not model judgment.  Once the
        # configured bound is exhausted, hand the case to a recruiter while
        # retaining the last trusted canonical values.
        if reconciliation.issues:
            issue_fields = {
                issue.field for issue in reconciliation.issues if issue.field is not None
            }
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
                    issues=reconciliation.issues,
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
        if decision.status is ScreeningStatus.DISQUALIFIED:
            return ConversationTurnResult(
                next_state,
                decision,
                self._disqualification_message(language, decision),
                changed_fields=reconciliation.changed_fields,
            )
        if decision.status is ScreeningStatus.QUALIFIED:
            return ConversationTurnResult(
                next_state,
                decision,
                self._qualified_message(language),
                changed_fields=reconciliation.changed_fields,
            )
        if "retry_limit" in decision.reason_codes:
            return ConversationTurnResult(
                next_state,
                decision,
                self._review_message(language),
                changed_fields=reconciliation.changed_fields,
            )

        if reconciliation.issues:
            field = reconciliation.issues[0].field or self.screening_engine.next_field(next_state)
            if field is not None:
                next_state.current_field = field
            prompt = self._clarification(language, field)
        elif next_state.pending_confirmation is not None:
            next_state.current_field = next_state.pending_confirmation.field
            prompt = self._pending_prompt(language, next_state.pending_confirmation)
        else:
            field = self.screening_engine.next_field(next_state)
            next_state.current_field = field
            prompt = self._confirmation(language) if field is None else _PROMPTS[language][field]

        if faq_answer:
            prompt = f"{faq_answer.strip()}\n\n{prompt}"
        elif (
            interpretation.intent.name in {"QUESTION", "MIXED"}
            and interpretation.response_requested
        ):
            prompt = f"{self._unknown_question(language)}\n\n{prompt}"

        return ConversationTurnResult(
            next_state,
            decision,
            prompt,
            next_field=next_state.current_field,
            changed_fields=reconciliation.changed_fields,
            security_event=reconciliation.security_event,
            faq_answered=bool(faq_answer),
        )

    @staticmethod
    def _confirmation(language: Language) -> str:
        if language is Language.EN:
            return "Please review the information above. Is everything correct?"
        return "Revisa la información anterior. ¿Es todo correcto?"

    @staticmethod
    def _clarification(language: Language, field: ScreeningField | None) -> str:
        if field is ScreeningField.LOCATION:
            return (
                "Could you specify the city and service area more precisely?"
                if language is Language.EN
                else "¿Puedes indicar la ciudad y la zona de servicio con más precisión?"
            )
        if field is ScreeningField.DRIVERS_LICENSE:
            return (
                "Please answer yes or no about your valid driver's licence."
                if language is Language.EN
                else "Responde sí o no sobre si tienes una licencia de conducir vigente."
            )
        if field is ScreeningField.DELIVERY_EXPERIENCE:
            return (
                "Please provide the number of years of delivery experience."
                if language is Language.EN
                else "Indica cuántos años de experiencia en reparto tienes."
            )
        if field is ScreeningField.START_AVAILABILITY:
            return (
                "What date or time period could you start?"
                if language is Language.EN
                else "¿En qué fecha o periodo podrías empezar?"
            )
        return (
            "Could you clarify that answer?"
            if language is Language.EN
            else "¿Puedes aclarar esa respuesta?"
        )

    @staticmethod
    def _qualified_message(language: Language) -> str:
        return (
            "Thanks! Your screening information meets the stated requirements. A recruiter will review it and contact you about next steps."
            if language is Language.EN
            else "¡Gracias! Tu información cumple los requisitos indicados. Una persona reclutadora la revisará y contactará contigo sobre los siguientes pasos."
        )

    @staticmethod
    def _disqualification_message(language: Language, decision: ScreeningDecision) -> str:
        if "no_drivers_license" in decision.reason_codes:
            return (
                "Thanks for your time. This role requires a valid driver's licence, so we cannot continue this screening."
                if language is Language.EN
                else "Gracias por tu tiempo. Este puesto requiere una licencia de conducir vigente, así que no podemos continuar con esta evaluación."
            )
        return (
            "Thanks for your time. This role is currently limited to the configured service areas, so we cannot continue this screening."
            if language is Language.EN
            else "Gracias por tu tiempo. Actualmente este puesto está limitado a las zonas de servicio configuradas, así que no podemos continuar con esta evaluación."
        )

    @staticmethod
    def _opt_out_message(language: Language) -> str:
        return (
            "Understood. We’ll close this screening. Thank you for your time."
            if language is Language.EN
            else "Entendido. Cerraremos esta evaluación. Gracias por tu tiempo."
        )

    @staticmethod
    def _security_message(language: Language, state: ScreeningState) -> str:
        next_field = state.current_field
        question = _PROMPTS[language].get(next_field, "") if next_field else ""
        base = (
            "I can help with the delivery-driver screening, but I can’t provide internal instructions or change the criteria."
            if language is Language.EN
            else "Puedo ayudarte con la evaluación del puesto de repartidor/a, pero no puedo compartir instrucciones internas ni cambiar los criterios."
        )
        return f"{base}\n\n{question}" if question else base

    @staticmethod
    def _sensitive_message(language: Language, state: ScreeningState) -> str:
        next_field = state.current_field
        question = _PROMPTS[language].get(next_field, "") if next_field else ""
        base = (
            "For your privacy, please do not share passwords, payment details, contact details, or government ID here."
            if language is Language.EN
            else "Por tu privacidad, no compartas aquí contraseñas, datos de pago, datos de contacto ni documentos de identidad."
        )
        return f"{base}\n\n{question}" if question else base

    @staticmethod
    def _unknown_question(language: Language) -> str:
        return (
            "I don’t have that information, but a recruiter can follow up."
            if language is Language.EN
            else "No tengo esa información, pero una persona reclutadora puede ayudarte después."
        )

    @staticmethod
    def _review_message(language: Language) -> str:
        return (
            "I’m unable to verify that detail after a few attempts. A recruiter will review your application."
            if language is Language.EN
            else "No he podido verificar ese dato después de varios intentos. Una persona reclutadora revisará tu solicitud."
        )

    @staticmethod
    def _pending_prompt(language: Language, pending: PendingConfirmation) -> str:
        """Render a pending correction/suggestion in the active language."""

        if pending.reason == "service_area_suggestion":
            proposed = pending.proposed_value
            proposed_mapping = cast(dict[str, Any], proposed) if isinstance(proposed, dict) else {}
            name = proposed_mapping.get("matched_name")
            if name:
                return (
                    f"Do you mean {name}? Please answer yes or no."
                    if language is Language.EN
                    else f"¿Te refieres a {name}? Responde sí o no."
                )
        if pending.field is ScreeningField.LOCATION:
            return (
                "Please confirm this service area."
                if language is Language.EN
                else "Confirma esta zona de servicio, por favor."
            )
        if pending.field is ScreeningField.DRIVERS_LICENSE:
            return (
                "Please confirm your driver's licence answer."
                if language is Language.EN
                else "Confirma tu respuesta sobre la licencia de conducir."
            )
        return (
            "I heard a different answer. Would you like to replace the previous information?"
            if language is Language.EN
            else "He recibido una respuesta distinta. ¿Quieres sustituir la información anterior?"
        )
