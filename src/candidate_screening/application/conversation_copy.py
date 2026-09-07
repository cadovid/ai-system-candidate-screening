"""Deterministic, localized copy for :class:`ResponsePlan` values.

The interpreter is allowed to identify facts and conversational intent, but it
does not get to author candidate-facing screening decisions.  This module is
the other half of that boundary: a small response plan is rendered into
bounded English or Spanish copy using only canonical state and trusted
context.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from candidate_screening.domain.enums import (
    AvailabilityType,
    Language,
    SchedulePreference,
    ScreeningField,
)
from candidate_screening.domain.models import PendingConfirmation, ScreeningState

from .response_plan import ResponseKind, ResponsePlan, VariantSelector, choose_variant

_PROMPTS: Mapping[Language, Mapping[ScreeningField, str]] = {
    Language.ES: {
        ScreeningField.FULL_NAME: "Para empezar, ¿cuál es tu nombre completo?",
        ScreeningField.DRIVERS_LICENSE: "¿Tienes una licencia de conducir vigente?",
        ScreeningField.LOCATION: "¿En qué ciudad y zona prefieres repartir?",
        ScreeningField.AVAILABILITY: "¿Qué disponibilidad te encaja mejor: tiempo completo, tiempo parcial o fines de semana?",
        ScreeningField.PREFERRED_SCHEDULE: "¿Qué horario te viene mejor: mañana, tarde, noche o flexible?",
        ScreeningField.DELIVERY_EXPERIENCE: "¿Cuánta experiencia tienes en reparto? Puedes contarme los años y, si quieres, las plataformas.",
        ScreeningField.START_AVAILABILITY: "¿Cuándo estarías disponible para empezar?",
    },
    Language.EN: {
        ScreeningField.FULL_NAME: "To get started, what's your full name?",
        ScreeningField.DRIVERS_LICENSE: "Do you have a valid driver's licence?",
        ScreeningField.LOCATION: "Which city and area would you prefer to deliver in?",
        ScreeningField.AVAILABILITY: "What availability suits you best: full-time, part-time, or weekends?",
        ScreeningField.PREFERRED_SCHEDULE: "Which schedule suits you best: morning, afternoon, evening, or flexible?",
        ScreeningField.DELIVERY_EXPERIENCE: "How much delivery experience do you have? You can share the years and any platforms you've used.",
        ScreeningField.START_AVAILABILITY: "When would you be available to start?",
    },
}


def _choose(
    plan: ResponsePlan,
    variants: Sequence[str],
    *,
    selector: VariantSelector | Callable[[str], int] | None,
    suffix: str,
) -> str:
    key = f"{plan.variant_seed}:{suffix}"
    return choose_variant(key, variants, selector=selector)


def _prompt(language: Language, field: ScreeningField | None) -> str:
    if field is None:
        return (
            "Please review the information above. Is everything correct?"
            if language is Language.EN
            else "Revisa la información anterior. ¿Es todo correcto?"
        )
    return _PROMPTS[language][field]


def _review_summary(language: Language, state: ScreeningState | None) -> str:
    """Render the canonical facts the candidate is being asked to confirm."""

    if state is None:
        return _prompt(language, None)

    availability_labels = {
        Language.EN: {
            AvailabilityType.FULL_TIME: "full-time",
            AvailabilityType.PART_TIME: "part-time",
            AvailabilityType.WEEKENDS: "weekends",
        },
        Language.ES: {
            AvailabilityType.FULL_TIME: "tiempo completo",
            AvailabilityType.PART_TIME: "tiempo parcial",
            AvailabilityType.WEEKENDS: "fines de semana",
        },
    }
    schedule_labels = {
        Language.EN: {
            SchedulePreference.MORNING: "morning",
            SchedulePreference.AFTERNOON: "afternoon",
            SchedulePreference.EVENING: "evening",
            SchedulePreference.FLEXIBLE: "flexible",
        },
        Language.ES: {
            SchedulePreference.MORNING: "mañana",
            SchedulePreference.AFTERNOON: "tarde",
            SchedulePreference.EVENING: "noche",
            SchedulePreference.FLEXIBLE: "flexible",
        },
    }
    location = state.location.matched_name or state.location.raw_value or state.location.city or "—"
    availability = (
        ", ".join(availability_labels[language][item] for item in state.availability.value)
        if state.availability is not None
        else "—"
    )
    schedule = (
        schedule_labels[language][state.preferred_schedule.value]
        if state.preferred_schedule is not None
        else "—"
    )
    if state.delivery_experience is None:
        experience = "—"
    else:
        years = f"{state.delivery_experience.years:g}"
        platforms = ", ".join(state.delivery_experience.platforms)
        if language is Language.EN:
            experience = f"{years} years" + (f" ({platforms})" if platforms else "")
        else:
            experience = f"{years} años" + (f" ({platforms})" if platforms else "")

    if language is Language.EN:
        lines = (
            "Please review what I have recorded:",
            f"• Full name: {state.full_name.value if state.full_name else '—'}",
            f"• Valid driver's licence: {'Yes' if state.drivers_license and state.drivers_license.value else 'No'}",
            f"• Delivery area: {location}",
            f"• Availability: {availability}",
            f"• Preferred schedule: {schedule}",
            f"• Delivery experience: {experience}",
            f"• Available to start: {state.start_availability.raw_value if state.start_availability else '—'}",
            "Is everything correct?",
        )
    else:
        lines = (
            "Revisa los datos que he anotado:",
            f"• Nombre completo: {state.full_name.value if state.full_name else '—'}",
            f"• Licencia de conducir vigente: {'Sí' if state.drivers_license and state.drivers_license.value else 'No'}",
            f"• Zona de reparto: {location}",
            f"• Disponibilidad: {availability}",
            f"• Horario preferido: {schedule}",
            f"• Experiencia en reparto: {experience}",
            f"• Disponibilidad para empezar: {state.start_availability.raw_value if state.start_availability else '—'}",
            "¿Está todo correcto?",
        )
    return "\n".join(lines)


def _prompt_variants(language: Language, field: ScreeningField | None) -> tuple[str, ...]:
    """Return a small set of direct prompts with optional natural transitions.

    The canonical question remains intact in every variant.  This keeps the
    requested field and any yes/no or option guidance stable while allowing a
    response after an answer or FAQ to sound less like a static form.  Final
    review copy is intentionally single-variant and is handled separately.
    """

    base = _prompt(language, field)
    if field is None:
        return (base,)
    if language is Language.EN:
        return (
            base,
            f"Thanks — {base}",
            f"Got it. {base}",
        )
    return (
        base,
        f"Gracias. {base}",
        f"Perfecto. {base}",
    )


def _clarification(
    language: Language,
    field: ScreeningField | None,
    understood: str | None = None,
) -> str:
    if understood:
        if field is ScreeningField.PREFERRED_SCHEDULE:
            return (
                f"I heard {understood}. Should I record that as your preferred schedule?"
                if language is Language.EN
                else f"He entendido {understood}. ¿Lo anoto como tu horario preferido?"
            )
        if field is ScreeningField.AVAILABILITY:
            return (
                f"I heard {understood}. Is that the availability that suits you best?"
                if language is Language.EN
                else f"He entendido {understood}. ¿Es la disponibilidad que mejor te encaja?"
            )
        if field is ScreeningField.DELIVERY_EXPERIENCE:
            return (
                f"I caught {understood}. Could you give me one clear number of years?"
                if language is Language.EN
                else f"He anotado {understood}. ¿Puedes darme un número claro de años?"
            )
        if field is ScreeningField.START_AVAILABILITY:
            return (
                f"I heard {understood}. Could you confirm when you could start?"
                if language is Language.EN
                else f"He entendido {understood}. ¿Puedes confirmar cuándo podrías empezar?"
            )
        if field is ScreeningField.LOCATION:
            return (
                f"I heard {understood}. Could you specify the service area more precisely?"
                if language is Language.EN
                else f"He entendido {understood}. ¿Puedes concretar la zona de servicio?"
            )
        field_label = field.value.replace("_", " ") if field is not None else "that answer"
        return (
            f"I heard {understood}. Could you confirm {field_label}?"
            if language is Language.EN
            else f"He entendido {understood}. ¿Puedes confirmar {field_label}?"
        )
    if field is ScreeningField.LOCATION:
        return (
            "Could you specify the city and service area more precisely?"
            if language is Language.EN
            else "¿Puedes indicar la ciudad y la zona de servicio con más precisión?"
        )
    if field is ScreeningField.DRIVERS_LICENSE:
        return (
            "Just to confirm, do you have a valid driver's licence?"
            if language is Language.EN
            else "Solo para confirmarlo, ¿tienes una licencia de conducir vigente?"
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
    labels = {
        Language.EN: {
            ScreeningField.FULL_NAME: "your full name",
            ScreeningField.AVAILABILITY: "which option fits you best: full-time, part-time, or weekends",
            ScreeningField.PREFERRED_SCHEDULE: "which schedule works best: morning, afternoon, evening, or flexible",
        },
        Language.ES: {
            ScreeningField.FULL_NAME: "tu nombre completo",
            ScreeningField.AVAILABILITY: "qué opción te encaja mejor: tiempo completo, tiempo parcial o fines de semana",
            ScreeningField.PREFERRED_SCHEDULE: "qué horario te viene mejor: mañana, tarde, noche o flexible",
        },
    }
    label = labels[language].get(field) if field is not None else None
    if label:
        return (
            f"Could you clarify {label}?"
            if language is Language.EN
            else f"¿Puedes aclarar {label}?"
        )
    return (
        "Could you clarify that answer?"
        if language is Language.EN
        else "¿Puedes aclarar esa respuesta?"
    )


def _clarification_variants(
    language: Language,
    field: ScreeningField | None,
    understood: str | None = None,
) -> tuple[str, ...]:
    """Return bounded, action-oriented clarification copy."""

    base = _clarification(language, field, understood)
    if language is Language.EN:
        return (
            base,
            f"Just so I get that right: {base}",
            f"Thanks — one quick clarification. {base}",
        )
    return (
        base,
        f"Solo para asegurarme de entenderte bien: {base}",
        f"Gracias; una aclaración rápida. {base}",
    )


def _bounded_text(value: Any, *, limit: int = 200) -> str:
    """Return a compact display value without exposing nested proposal data."""

    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:limit]


def _proposal_mapping(pending: PendingConfirmation) -> Mapping[str, Any]:
    proposed = pending.proposed_value
    return cast(Mapping[str, Any], proposed) if isinstance(proposed, Mapping) else {}


def _location_proposal_copy(
    language: Language,
    pending: PendingConfirmation,
) -> str:
    """Describe the bounded location proposal before a decision-impact check."""

    data = _proposal_mapping(pending)
    raw = _bounded_text(data.get("raw_value") or data.get("normalized_value"))
    city = _bounded_text(data.get("city"))
    zone = _bounded_text(data.get("zone") or data.get("matched_name"))
    if language is Language.EN:
        if raw and city and zone:
            understood = f"your location as “{raw}” in {city}, {zone}"
        elif raw and city:
            understood = f"your location as “{raw}” in {city}"
        elif raw and zone:
            understood = f"your delivery area as “{raw}” ({zone})"
        elif raw:
            understood = f"your delivery area as “{raw}”"
        elif city and zone:
            understood = f"the location {city}, {zone}"
        elif city:
            understood = f"the location {city}"
        elif zone:
            understood = f"the service area {zone}"
        else:
            understood = "the location you provided"
        return (
            f"I understood {understood}. Please confirm this is the service area "
            "you can deliver in. Answer yes or no."
        )
    if raw and city and zone:
        understood = f"tu ubicación como «{raw}», en {city}, {zone}"
    elif raw and city:
        understood = f"tu ubicación como «{raw}», en {city}"
    elif raw and zone:
        understood = f"tu zona de reparto como «{raw}» ({zone})"
    elif raw:
        understood = f"tu zona de reparto como «{raw}»"
    elif city and zone:
        understood = f"la ubicación {city}, {zone}"
    elif city:
        understood = f"la ubicación {city}"
    elif zone:
        understood = f"la zona de servicio {zone}"
    else:
        understood = "la ubicación indicada"
    return (
        f"He entendido {understood}. Confirma que esta es la zona de servicio "
        "donde puedes repartir. Responde sí o no."
    )


def _license_proposal_copy(language: Language, pending: PendingConfirmation) -> str:
    """State the understood licence answer before asking for confirmation."""

    value = _proposal_mapping(pending).get("value")
    if language is Language.EN:
        if value is False:
            understood = "you do not have a valid driver's licence"
        elif value is True:
            understood = "you have a valid driver's licence"
        else:
            understood = "your valid driver's licence answer"
        return f"I understood that {understood}. Please confirm this answer. Reply yes or no."
    if value is False:
        understood = "no tienes una licencia de conducir vigente"
    elif value is True:
        understood = "tienes una licencia de conducir vigente"
    else:
        understood = "tu respuesta sobre la licencia de conducir vigente"
    return f"He entendido que {understood}. Confirma esta respuesta. Responde sí o no."


def _generic_proposal_display(language: Language, pending: PendingConfirmation) -> str:
    """Choose one bounded candidate-facing value, excluding evidence metadata."""

    data = _proposal_mapping(pending)
    if pending.field is ScreeningField.LOCATION:
        value = data.get("raw_value") or data.get("normalized_value") or data.get("city")
        return _bounded_text(value)
    if pending.field is ScreeningField.DRIVERS_LICENSE:
        value = data.get("value")
        if isinstance(value, bool):
            return (
                ("yes" if value else "no") if language is Language.EN else ("sí" if value else "no")
            )
    for key in ("value", "raw_value", "years", "date"):
        value = data.get(key)
        if isinstance(value, str):
            rendered = _bounded_text(value)
            if rendered:
                return rendered
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values = cast(Sequence[Any], value)
            rendered_items = [_bounded_text(item, limit=80) for item in values]
            rendered = ", ".join(item for item in rendered_items if item)
            if rendered:
                return rendered[:200]
    return ""


def _pending_text(
    language: Language,
    pending: PendingConfirmation | None,
    context: Mapping[str, Any],
) -> str:
    if pending is None:
        return _prompt(language, None)
    if pending.reason == "service_area_city":
        proposed = pending.proposed_value
        data: Mapping[str, Any] = (
            cast(Mapping[str, Any], proposed) if isinstance(proposed, Mapping) else {}
        )
        city = str(
            data.get("city")
            or context.get("city")
            or ("the city you provided" if language is Language.EN else "la ciudad indicada")
        )
        raw_zones = context.get("zones")
        zones_value: Sequence[Any] = (
            cast(Sequence[Any], raw_zones)
            if isinstance(raw_zones, Sequence) and not isinstance(raw_zones, (str, bytes))
            else ()
        )
        zones = [str(item) for item in zones_value if str(item).strip()]
        if not zones:
            raw_area_ids = data.get("service_area_ids", [])
            area_ids: Sequence[Any] = (
                cast(Sequence[Any], raw_area_ids)
                if isinstance(raw_area_ids, Sequence) and not isinstance(raw_area_ids, (str, bytes))
                else ()
            )
            zones = [str(item) for item in area_ids if str(item).strip()]
        zone_text = ", ".join(zones) or (
            "the configured areas" if language is Language.EN else "las zonas configuradas"
        )
        return (
            f"I understood {city}. The configured delivery areas in {city} are: {zone_text}. Can you deliver in any of these areas? Please answer yes or no."
            if language is Language.EN
            else f"He entendido {city}. Las zonas de reparto configuradas en {city} son: {zone_text}. ¿Puedes repartir en alguna de estas zonas? Responde sí o no."
        )
    if pending.reason == "service_area_suggestion":
        proposed = pending.proposed_value
        data: Mapping[str, Any] = (
            cast(Mapping[str, Any], proposed) if isinstance(proposed, Mapping) else {}
        )
        name = str(data.get("matched_name") or context.get("matched_name") or "").strip()
        if name:
            return (
                f"Do you mean {name}? Please answer yes or no."
                if language is Language.EN
                else f"¿Te refieres a {name}? Responde sí o no."
            )
    if pending.reason == "decision_impact_confirmation":
        if pending.field is ScreeningField.LOCATION:
            return _location_proposal_copy(language, pending)
        if pending.field is ScreeningField.DRIVERS_LICENSE:
            return _license_proposal_copy(language, pending)
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
    proposed = _generic_proposal_display(language, pending)
    if proposed:
        return (
            f"I understood the proposed replacement as “{proposed}”. Would you like to "
            "replace the previous information? Please answer yes or no."
            if language is Language.EN
            else f"He entendido que la nueva respuesta es «{proposed}». ¿Quieres sustituir "
            "la información anterior? Responde sí o no."
        )
    return (
        "I heard a different answer. Would you like to replace the previous information?"
        if language is Language.EN
        else "He recibido una respuesta distinta. ¿Quieres sustituir la información anterior?"
    )


def _terminal_copy(plan: ResponsePlan) -> str:
    language = plan.language
    context = plan.context
    if plan.kind is ResponseKind.QUALIFIED:
        return (
            "Thanks! Your screening information meets the stated requirements. A recruiter will review it and contact you about next steps."
            if language is Language.EN
            else "¡Gracias! Tu información cumple los requisitos indicados. Una persona reclutadora la revisará y contactará contigo sobre los siguientes pasos."
        )
    if plan.kind is ResponseKind.DISQUALIFIED:
        decision = plan.decision
        reasons = decision.reason_codes if decision is not None else ()
        if "no_drivers_license" in reasons:
            return (
                "Thanks for your time. This role requires a valid driver's licence, so we cannot continue this screening."
                if language is Language.EN
                else "Gracias por tu tiempo. Este puesto requiere una licencia de conducir vigente, así que no podemos continuar con esta evaluación."
            )
        raw = str(context.get("raw_value") or "the location you provided").strip()
        city = str(context.get("city") or "").strip()
        zones_value = context.get("zones")
        zones = (
            ", ".join(str(item) for item in cast(Sequence[Any], zones_value))
            if isinstance(zones_value, Sequence) and not isinstance(zones_value, (str, bytes))
            else ""
        )
        if city and zones:
            return (
                f"Thanks for your time. I understood your location as “{raw}” in {city}. The configured delivery areas in {city} are: {zones}. Because you cannot deliver in any of those areas, we cannot continue this screening."
                if language is Language.EN
                else f"Gracias por tu tiempo. He interpretado tu ubicación como «{raw}» en {city}. Las zonas de reparto configuradas en {city} son: {zones}. Como no puedes repartir en ninguna de ellas, no podemos continuar con esta evaluación."
            )
        return (
            f"Thanks for your time. I understood your delivery area as “{raw}”. It is not one of the configured service areas, so we cannot continue this screening."
            if language is Language.EN
            else f"Gracias por tu tiempo. He interpretado tu zona de reparto como «{raw}». No está entre las zonas de servicio configuradas, así que no podemos continuar con esta evaluación."
        )
    if plan.kind is ResponseKind.NEEDS_REVIEW:
        labels = {
            Language.EN: {
                ScreeningField.FULL_NAME: "full name",
                ScreeningField.DRIVERS_LICENSE: "licence answer",
                ScreeningField.LOCATION: "delivery area",
                ScreeningField.AVAILABILITY: "availability",
                ScreeningField.PREFERRED_SCHEDULE: "preferred schedule",
                ScreeningField.DELIVERY_EXPERIENCE: "delivery experience",
                ScreeningField.START_AVAILABILITY: "start date",
            },
            Language.ES: {
                ScreeningField.FULL_NAME: "nombre completo",
                ScreeningField.DRIVERS_LICENSE: "respuesta sobre la licencia",
                ScreeningField.LOCATION: "zona de reparto",
                ScreeningField.AVAILABILITY: "disponibilidad",
                ScreeningField.PREFERRED_SCHEDULE: "horario preferido",
                ScreeningField.DELIVERY_EXPERIENCE: "experiencia en reparto",
                ScreeningField.START_AVAILABILITY: "fecha de inicio",
            },
        }
        field_label = labels[language].get(plan.field) if plan.field else None
        if field_label:
            return (
                f"I couldn't confirm your {field_label} after a couple of tries. A recruiter will review your application."
                if language is Language.EN
                else f"No he podido confirmar tu {field_label} después de un par de intentos. Una persona reclutadora revisará tu solicitud."
            )
        return (
            "I’m unable to verify that detail after a few attempts. A recruiter will review your application."
            if language is Language.EN
            else "No he podido verificar ese dato después de varios intentos. Una persona reclutadora revisará tu solicitud."
        )
    if plan.kind is ResponseKind.OPT_OUT:
        return (
            "Understood. We’ll close this screening. Thank you for your time."
            if language is Language.EN
            else "Entendido. Cerraremos esta evaluación. Gracias por tu tiempo."
        )
    if plan.kind is ResponseKind.MAX_TURNS:
        return (
            "We have reached the maximum number of screening messages. A recruiter will review your application and follow up."
            if language is Language.EN
            else "Hemos alcanzado el número máximo de mensajes de la evaluación. Una persona reclutadora revisará tu solicitud y te contactará."
        )
    if plan.kind is ResponseKind.SECURITY:
        base = (
            "I can help with the delivery-driver screening, but I can’t provide internal instructions or change the criteria."
            if language is Language.EN
            else "Puedo ayudarte con la evaluación del puesto de repartidor/a, pero no puedo compartir instrucciones internas ni cambiar los criterios."
        )
    elif plan.kind is ResponseKind.SENSITIVE:
        base = (
            "For your privacy, please do not share passwords, payment details, contact details, or government ID here."
            if language is Language.EN
            else "Por tu privacidad, no compartas aquí contraseñas, datos de pago, datos de contacto ni documentos de identidad."
        )
    elif plan.kind is ResponseKind.TEMPORARY_FAILURE:
        base = (
            "I’m sorry, I’m temporarily unable to process that message. Please try again."
            if language is Language.EN
            else "Lo siento, no puedo procesar ese mensaje temporalmente. Inténtalo de nuevo."
        )
    else:
        return ""
    next_field = plan.field or (plan.state.current_field if plan.state else None)
    question = _PROMPTS[language].get(next_field, "") if next_field else ""
    return (
        f"{base}\n\n{question}"
        if question and plan.kind in {ResponseKind.SECURITY, ResponseKind.SENSITIVE}
        else base
    )


def render_response_plan(
    plan: ResponsePlan,
    *,
    selector: VariantSelector | Callable[[str], int] | None = None,
) -> str:
    """Render a response plan into bounded candidate-facing copy.

    FAQ text is deliberately bridged only for non-terminal conversation plans;
    terminal, privacy, and provider-failure responses are strict and cannot be
    prefixed by arbitrary question text.
    """

    if plan.kind in {
        ResponseKind.QUALIFIED,
        ResponseKind.DISQUALIFIED,
        ResponseKind.NEEDS_REVIEW,
        ResponseKind.OPT_OUT,
        ResponseKind.MAX_TURNS,
        ResponseKind.SECURITY,
        ResponseKind.SENSITIVE,
        ResponseKind.TEMPORARY_FAILURE,
    }:
        return _terminal_copy(plan)

    if plan.kind is ResponseKind.CLARIFICATION:
        body = _choose(
            plan,
            _clarification_variants(
                plan.language,
                plan.field,
                _bounded_text(plan.context.get("understood"), limit=300),
            ),
            selector=selector,
            suffix="clarification",
        )
    elif plan.kind is ResponseKind.PENDING_CONFIRMATION:
        body = _pending_text(plan.language, plan.pending, plan.context)
    elif plan.kind is ResponseKind.UNKNOWN_QUESTION:
        unknown = _choose(
            plan,
            (
                (
                    "I don’t have that information, but a recruiter can follow up."
                    if plan.language is Language.EN
                    else "No tengo esa información, pero una persona reclutadora puede ayudarte después."
                ),
                (
                    "I don’t have that information yet, but a recruiter can follow up."
                    if plan.language is Language.EN
                    else "Todavía no tengo esa información, pero una persona reclutadora puede ayudarte después."
                ),
                (
                    "I don’t have that information right now, but a recruiter can follow up."
                    if plan.language is Language.EN
                    else "Ahora no tengo esa información, pero una persona reclutadora puede ayudarte después."
                ),
            ),
            selector=selector,
            suffix="unknown_question",
        )
        body = unknown
        prompt = _choose(
            plan,
            _prompt_variants(plan.language, plan.field),
            selector=selector,
            suffix="unknown_question_prompt",
        )
        body = f"{body}\n\n{prompt}"
    elif plan.kind is ResponseKind.FAQ_BRIDGE:
        body = _choose(
            plan,
            _prompt_variants(plan.language, plan.field),
            selector=selector,
            suffix="faq_bridge_prompt",
        )
    elif plan.kind is ResponseKind.FAQ_OFFER:
        body = (
            "Before we finish, do you have any questions about the company, the role, or the hiring process?"
            if plan.language is Language.EN
            else "Antes de terminar, ¿tienes alguna pregunta sobre la empresa, el puesto o el proceso de selección?"
        )
    elif plan.kind is ResponseKind.FAQ_FOLLOWUP:
        if plan.context.get("has_question"):
            followup = (
                "Do you have any other questions?"
                if plan.language is Language.EN
                else "¿Tienes alguna otra pregunta?"
            )
            if plan.faq_answer:
                body = f"{plan.faq_answer.strip()}\n\n{followup}"
            else:
                unavailable = (
                    "I don't have confirmed information about that, so a recruiter can clarify it for you."
                    if plan.language is Language.EN
                    else "No tengo información confirmada sobre eso, así que una persona reclutadora podrá aclarártelo."
                )
                body = f"{unavailable}\n\n{followup}"
        else:
            body = (
                "Of course. What would you like to know?"
                if plan.language is Language.EN
                else "Claro. ¿Qué te gustaría saber?"
            )
    elif plan.kind is ResponseKind.FINAL_CONFIRMATION:
        body = _review_summary(plan.language, plan.state)
    else:
        body = _choose(
            plan,
            _prompt_variants(plan.language, plan.field),
            selector=selector,
            suffix="prompt",
        )

    if plan.faq_answer and plan.kind is not ResponseKind.FAQ_FOLLOWUP:
        body = f"{plan.faq_answer.strip()}\n\n{body}"
    return body


__all__ = ["render_response_plan"]
