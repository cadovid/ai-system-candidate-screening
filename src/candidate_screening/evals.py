"""Deterministic and live conversation evaluation helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from candidate_screening.ai.interpreter import InterpreterDependencies, InterpreterResult
from candidate_screening.ai.pydantic_ai import PydanticAIInterpreter
from candidate_screening.ai.schemas import (
    ExtractedDeliveryExperience,
    ExtractedLocation,
    ExtractedValue,
    StartAvailabilityExtraction,
    TurnIntent,
    TurnInterpretation,
)
from candidate_screening.application.conversation import ConversationController
from candidate_screening.application.faq import FAQCatalog
from candidate_screening.config import Settings
from candidate_screening.domain.enums import (
    AvailabilityType,
    Language,
    SchedulePreference,
    ScreeningField,
    ScreeningStatus,
)
from candidate_screening.domain.models import ScreeningState
from candidate_screening.domain.rules import ScreeningEngine
from candidate_screening.domain.service_areas import ServiceAreaMatcher

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIOS_PATH = _ROOT / "data" / "scenarios" / "scenarios.json"
DEFAULT_EVALS_PATH = _ROOT / "data" / "evals" / "eval_cases.json"
_YES = {"yes", "y", "sí", "si", "true", "correct", "correcto", "vale"}
_NO = {"no", "n", "false", "nope"}


@dataclass(frozen=True, slots=True)
class EvalScenario:
    id: str
    language: Language
    turns: tuple[str, ...]
    expected: Mapping[str, Any]
    description: str = ""
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvalScenario:
        turns_value: object = payload.get("turns", payload.get("messages", ()))
        normalized_turns: list[str] = []
        turns = (
            cast(Sequence[object], turns_value)
            if isinstance(turns_value, Sequence) and not isinstance(turns_value, str)
            else ()
        )
        for turn in turns:
            if isinstance(turn, str):
                normalized_turns.append(turn)
            elif isinstance(turn, Mapping):
                turn_mapping = cast(Mapping[str, Any], turn)
                if not isinstance(turn_mapping.get("content"), str) or str(
                    turn_mapping.get("role", "user")
                ) not in {"user", "candidate"}:
                    continue
                # Sample transcripts may include assistant messages; eval
                # turns should only feed candidate content to the interpreter.
                normalized_turns.append(str(turn_mapping["content"]))
        try:
            language = Language(str(payload.get("language", "es")))
        except ValueError:
            language = Language.ES
        expected_value = payload.get("expected", {})
        tags_value = payload.get("tags", ())
        expected = (
            dict(cast(Mapping[str, Any], expected_value))
            if isinstance(expected_value, Mapping)
            else {}
        )
        tags = (
            tuple(str(tag) for tag in cast(Sequence[object], tags_value) if str(tag).strip())
            if isinstance(tags_value, Sequence) and not isinstance(tags_value, str)
            else ()
        )
        return cls(
            id=str(payload.get("id", "scenario")),
            language=language,
            turns=tuple(normalized_turns),
            expected=expected,
            description=str(payload.get("description", "")),
            tags=tags,
        )


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    scenario: str
    expected_status: ScreeningStatus
    max_turns: int = 40
    security_event: bool = False
    retryable: bool = False
    expected_reason_codes: tuple[str, ...] = ()
    expected_fields: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())
    expected_language: Language | None = None
    expected_language_trace: tuple[Language, ...] = ()
    expected_faq_answered: bool | None = None
    expected_faq_contains: tuple[str, ...] = ()
    expected_correction_field: str | None = None
    expected_correction_confirmed: bool | None = None
    expected_error: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvalCase:
        expected_value = payload.get("expected", {})
        expected: Mapping[str, Any] = (
            cast(Mapping[str, Any], expected_value) if isinstance(expected_value, Mapping) else {}
        )
        try:
            status = ScreeningStatus(
                str(payload.get("expected_status", expected.get("status", "in_progress")))
            )
        except ValueError:
            status = ScreeningStatus.IN_PROGRESS

        def _strings(value: object) -> tuple[str, ...]:
            if isinstance(value, str):
                return (value,)
            if not isinstance(value, Sequence):
                return ()
            return tuple(str(item) for item in cast(Sequence[object], value) if str(item).strip())

        reason_codes = _strings(
            payload.get("expected_reason_codes", expected.get("reason_codes", ()))
        )
        fields_value = payload.get(
            "expected_fields",
            payload.get(
                "canonical_fields",
                expected.get("canonical_fields", expected.get("fields", {})),
            ),
        )
        expected_fields: dict[str, Any] = (
            dict(cast(Mapping[str, Any], fields_value)) if isinstance(fields_value, Mapping) else {}
        )
        expected_language_value = payload.get("expected_language", expected.get("language"))
        try:
            expected_language = (
                Language(str(expected_language_value))
                if expected_language_value is not None
                else None
            )
        except ValueError:
            expected_language = None
        trace_value = payload.get("expected_language_trace", expected.get("language_trace", ()))
        language_trace: tuple[Language, ...] = ()
        if isinstance(trace_value, Sequence) and not isinstance(trace_value, str):
            normalized_trace: list[Language] = []
            for value in cast(Sequence[object], trace_value):
                try:
                    normalized_trace.append(Language(str(value)))
                except ValueError:
                    continue
            language_trace = tuple(normalized_trace)
        faq_contains = _strings(
            payload.get("expected_faq_contains", expected.get("faq_contains", ()))
        )
        faq_answered_value = payload.get("expected_faq_answered", expected.get("faq_answered"))
        faq_answered = faq_answered_value if isinstance(faq_answered_value, bool) else None
        correction_value = payload.get("correction", expected.get("correction", {}))
        correction: Mapping[str, Any] = (
            cast(Mapping[str, Any], correction_value)
            if isinstance(correction_value, Mapping)
            else {}
        )
        correction_field_value = payload.get("expected_correction_field", correction.get("field"))
        correction_field = (
            str(correction_field_value) if correction_field_value is not None else None
        )
        correction_confirmed_value = payload.get(
            "expected_correction_confirmed", correction.get("confirmed")
        )
        correction_confirmed = (
            correction_confirmed_value if isinstance(correction_confirmed_value, bool) else None
        )
        expected_error_value = payload.get(
            "expected_error", expected.get("error_type", expected.get("error"))
        )
        return cls(
            id=str(payload.get("id", payload.get("scenario", "case"))),
            scenario=str(payload.get("scenario", payload.get("id", ""))),
            expected_status=status,
            max_turns=max(1, int(payload.get("max_turns", 40))),
            security_event=bool(payload.get("security_event", False)),
            retryable=bool(payload.get("retryable", False)),
            expected_reason_codes=reason_codes,
            expected_fields=expected_fields,
            expected_language=expected_language,
            expected_language_trace=language_trace,
            expected_faq_answered=faq_answered,
            expected_faq_contains=faq_contains,
            expected_correction_field=correction_field,
            expected_correction_confirmed=correction_confirmed,
            expected_error=(str(expected_error_value) if expected_error_value else None),
        )


@dataclass(frozen=True, slots=True)
class EvalResult:
    id: str
    passed: bool
    expected_status: ScreeningStatus
    actual_status: ScreeningStatus
    turns: int
    security_event: bool = False
    retryable: bool = False
    error: str | None = None
    reason_codes: tuple[str, ...] = ()
    expected_reason_codes: tuple[str, ...] = ()
    canonical_fields: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())
    expected_fields: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())
    language: Language = Language.ES
    expected_language: Language | None = None
    language_trace: tuple[Language, ...] = ()
    expected_language_trace: tuple[Language, ...] = ()
    faq_answered: bool = False
    faq_answers: tuple[str, ...] = ()
    expected_faq_answered: bool | None = None
    correction_fields: tuple[str, ...] = ()
    correction_confirmed: bool = False
    expected_correction_field: str | None = None
    expected_correction_confirmed: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "passed": self.passed,
            "expected_status": self.expected_status.value,
            "actual_status": self.actual_status.value,
            "turns": self.turns,
            "security_event": self.security_event,
            "retryable": self.retryable,
            "error": self.error,
            "reason_codes": list(self.reason_codes),
            "expected_reason_codes": list(self.expected_reason_codes),
            "canonical_fields": dict(self.canonical_fields),
            "expected_fields": dict(self.expected_fields),
            "language": self.language.value,
            "expected_language": (
                self.expected_language.value if self.expected_language is not None else None
            ),
            "language_trace": [language.value for language in self.language_trace],
            "expected_language_trace": [
                language.value for language in self.expected_language_trace
            ],
            "faq_answered": self.faq_answered,
            "faq_answers": list(self.faq_answers),
            "expected_faq_answered": self.expected_faq_answered,
            "correction_fields": list(self.correction_fields),
            "correction_confirmed": self.correction_confirmed,
            "expected_correction_field": self.expected_correction_field,
            "expected_correction_confirmed": self.expected_correction_confirmed,
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    mode: str
    results: tuple[EvalResult, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "results": [result.as_dict() for result in self.results],
        }


class AsyncInterpreter(Protocol):
    async def interpret(
        self,
        message: str,
        dependencies: InterpreterDependencies,
        *,
        message_history: Sequence[Any] = (),
    ) -> InterpreterResult: ...


def _read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as file:
        return json.load(file)


def load_scenarios(path: str | Path = DEFAULT_SCENARIOS_PATH) -> dict[str, EvalScenario]:
    payload: object = _read_json(path)
    if isinstance(payload, Mapping):
        payload = cast(Mapping[str, Any], payload).get("scenarios", [])
    if not isinstance(payload, list):
        raise ValueError("scenario data must be a list or an object with scenarios")
    payload_items = cast(list[object], payload)
    scenarios = {
        scenario.id: scenario
        for scenario in (
            EvalScenario.from_dict(cast(Mapping[str, Any], item))
            for item in payload_items
            if isinstance(item, Mapping)
        )
    }
    if not scenarios:
        raise ValueError("scenario data must contain at least one scenario")
    return scenarios


def load_eval_cases(path: str | Path = DEFAULT_EVALS_PATH) -> tuple[EvalCase, ...]:
    payload: object = _read_json(path)
    if isinstance(payload, Mapping):
        payload_mapping = cast(Mapping[str, Any], payload)
        payload = payload_mapping.get("cases", payload_mapping.get("evals", []))
    if not isinstance(payload, list):
        raise ValueError("eval data must be a list or an object with cases")
    payload_items = cast(list[object], payload)
    cases = tuple(
        EvalCase.from_dict(cast(Mapping[str, Any], item))
        for item in payload_items
        if isinstance(item, Mapping)
    )
    if not cases:
        raise ValueError("eval data must contain at least one case")
    return cases


def _words(message: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[\wÀ-ÿ]+", message)}


def _yes_no(message: str) -> bool | None:
    words = _words(message)
    if words & _YES and words & _NO:
        return None
    if words & _YES:
        return True
    if words & _NO:
        return False
    return None


def _explicit_language(message: str) -> Language | None:
    lowered = message.casefold()
    if re.search(r"\b(?:english|inglés|ingles|in english|en inglés|en ingles)\b", lowered):
        return Language.EN
    if re.search(r"\b(?:spanish|español|espanol|in spanish|en español|en espanol)\b", lowered):
        return Language.ES
    return None


def _language_for_message(message: str, state: ScreeningState) -> Language:
    """Choose a stable fixture language without pretending to be a detector."""

    explicit = _explicit_language(message)
    if explicit is not None:
        return explicit
    words = _words(message)
    english = words & {
        "yes",
        "please",
        "full",
        "part",
        "time",
        "morning",
        "afternoon",
        "evening",
        "night",
        "years",
        "start",
        "week",
        "asap",
        "licence",
        "license",
        "name",
        "location",
        "schedule",
        "experience",
    }
    spanish = words & {
        "sí",
        "si",
        "por",
        "favor",
        "completo",
        "parcial",
        "mañana",
        "tarde",
        "noche",
        "años",
        "empezar",
        "semana",
        "licencia",
        "nombre",
        "ciudad",
        "horario",
        "experiencia",
    }
    if len(english) > len(spanish):
        return Language.EN
    if len(spanish) > len(english):
        return Language.ES
    return state.preferred_language


def _common_interpretation(message: str, state: ScreeningState) -> dict[str, Any]:
    language = _language_for_message(message, state)
    values: dict[str, Any] = {
        "detected_language": language,
        "language_confidence": 0.9,
        "intent": TurnIntent.ANSWER,
    }
    explicit = _explicit_language(message)
    if explicit is not None:
        values["explicit_language"] = explicit
    return values


def _extract_name(value: str) -> str:
    value = re.sub(
        r"^\s*(?:my\s+name\s+is|name\s+is|me\s+llamo|mi\s+nombre\s+es)\s+", "", value, flags=re.I
    )
    return value.strip(' \t,.;:|-"')


def _label_value(message: str, labels: str) -> str | None:
    match = re.search(
        rf"(?:^|[;,|])\s*(?:{labels})\s*(?:is|es|=|:)\s*([^,;|]+)",
        message,
        re.I,
    )
    if match is None:
        # Also allow a natural-language label in the middle of a sentence.
        match = re.search(rf"\b(?:{labels})\s*(?:is|es|=|:)\s*([^,;|]+)", message, re.I)
    return _extract_name(match.group(1)) if match else None


def _availability_value(value: str) -> list[AvailabilityType]:
    words = _words(value)
    result: list[AvailabilityType] = []
    if words & {"full", "completo", "completa", "tiempo"} and (
        "full" in words or "completo" in words or "completa" in words
    ):
        result.append(AvailabilityType.FULL_TIME)
    if words & {"part", "parcial"}:
        result.append(AvailabilityType.PART_TIME)
    if words & {"weekend", "weekends", "fin", "semana"} and (
        "weekend" in words or "weekends" in words or "fin" in words or "semana" in words
    ):
        result.append(AvailabilityType.WEEKENDS)
    return list(dict.fromkeys(result))


def _schedule_value(value: str) -> SchedulePreference | None:
    words = _words(value)
    if words & {"morning", "mañana"}:
        return SchedulePreference.MORNING
    if words & {"afternoon", "tarde"}:
        return SchedulePreference.AFTERNOON
    if words & {"evening", "night", "noche"}:
        return SchedulePreference.EVENING
    if "flexible" in words:
        return SchedulePreference.FLEXIBLE
    return None


def _experience_value(value: str) -> ExtractedDeliveryExperience:
    match = re.search(r"(?:^|\D)(\d+(?:[.,]\d+)?)\s*(?:years?|años?)?", value, re.I)
    years = float(match.group(1).replace(",", ".")) if match else None
    lowered = value.casefold()
    platforms = [
        platform
        for platform in ("Glovo", "Uber Eats", "Deliveroo")
        if platform.casefold() in lowered
    ]
    return ExtractedDeliveryExperience(
        years=years,
        platforms=platforms,
        provided=years is not None,
        evidence=value[:500],
    )


def _start_value(value: str) -> StartAvailabilityExtraction:
    words = _words(value)
    precision = "unknown"
    if words & {"asap", "ahora", "ya"} or "lo antes posible" in value.casefold():
        precision = "asap"
    elif words & {"week", "semana"}:
        precision = "week"
    elif words & {"month", "mes"}:
        precision = "month"
    return StartAvailabilityExtraction(
        raw_value=value.strip(),
        precision=precision,
        provided=bool(value.strip()),
        evidence=value[:500],
    )


def _correction_requested(message: str) -> bool:
    return bool(
        re.search(
            r"\b(?:actually|instead|correction|correct(?:ion)?|change|update|me equivoqué|quise decir|en realidad)\b",
            message,
            re.I,
        )
    )


def _question_or_off_topic(message: str, state: ScreeningState) -> TurnInterpretation | None:
    lowered = message.casefold().strip()
    words = _words(message)
    if "?" in message or re.match(
        r"^(?:what|how|why|can|do|is|are|qué|que|cómo|como|cuál|cual|puedo)\b", lowered
    ):
        common = _common_interpretation(message, state)
        common.update(
            intent=TurnIntent.QUESTION,
            response_requested=True,
            candidate_questions=[message[:500]],
        )
        return TurnInterpretation(**common)
    if words & {"joke", "weather", "favorite", "politics", "hobby", "meaning"}:
        common = _common_interpretation(message, state)
        common.update(intent=TurnIntent.OFF_TOPIC, response_requested=True)
        return TurnInterpretation(**common)
    return None


def _deterministic_interpretation(message: str, state: ScreeningState) -> TurnInterpretation:
    """Interpret fixture text into a typed patch, never into a screening decision.

    This intentionally small parser supports the reviewable fixtures below:
    labelled multi-field answers, bilingual controls, corrections, FAQ
    interruptions, and explicit confirmation.  It is not used in production.
    """

    lowered = message.casefold().strip()
    words = _words(message)
    if lowered == "__provider_failure__":
        raise RuntimeError("fixture provider failure")
    if lowered == "__malformed_output__":
        raise ValueError("fixture malformed model output")
    if words & {"stop", "unsubscribe", "cancel", "baja", "salir", "parar"}:
        return TurnInterpretation(intent=TurnIntent.OPT_OUT, opt_out_requested=True)
    if (
        "ignore previous" in lowered
        or "system prompt" in lowered
        or "reveal the prompt" in lowered
        or "disregard instructions" in lowered
    ):
        return TurnInterpretation(
            intent=TurnIntent.PROMPT_INJECTION,
            prompt_injection_detected=True,
        )
    if words & {"password", "contraseña", "passwd", "cvv", "iban", "api_key", "token"}:
        return TurnInterpretation(
            intent=TurnIntent.OFF_TOPIC,
            sensitive_data_detected=True,
        )

    question = _question_or_off_topic(message, state)
    if question is not None:
        return question

    common = _common_interpretation(message, state)
    explicit = _explicit_language(message)
    yes_no = _yes_no(message)
    # A pending proposal is resolved by an explicit yes/no.  This branch is
    # what makes correction and fuzzy-location fixtures exercise the real
    # confirmation path instead of being mistaken for a field answer.
    if state.pending_confirmation is not None and yes_no is not None:
        common["confirmation"] = yes_no
        return TurnInterpretation(**common)

    # A language-control message updates only the preferred language.  Do not
    # accidentally save "Please use English" as a candidate's name.
    if (
        explicit is not None
        and not re.search(
            r"\b(?:name|nombre|licen[cs]e|licencia|location|city|ciudad|area|zona|availability|disponibilidad|schedule|horario|experience|experiencia|start|empezar)\b",
            lowered,
        )
        and not re.search(r"\b(?:yes|sí|si|no)\b", lowered)
    ):
        common["intent"] = TurnIntent.OFF_TOPIC
        return TurnInterpretation(**common)

    correction = _correction_requested(message)
    evidence = message[:500]
    fields: dict[str, Any] = {}
    labelled_name = _label_value(message, r"full\s+name|name|nombre")
    labelled_license = _label_value(message, r"driver'?s?\s+licen[cs]e|licen[cs]e|licencia")
    labelled_location = _label_value(message, r"location|city|area|ubicación|ubicacion|ciudad|zona")
    labelled_availability = _label_value(message, r"availability|disponibilidad")
    labelled_schedule = _label_value(message, r"schedule|horario|turno")
    labelled_experience = _label_value(message, r"experience|experiencia")
    labelled_start = _label_value(
        message, r"start|start\s+date|empezar|incorporación|incorporacion"
    )

    # Natural-language names are useful when the name is the only answer or
    # is introduced with "I'm/me llamo".
    if labelled_name is None:
        match_name = re.search(
            r"(?:\b(?:i['’]?m|i\s+am|my\s+name\s+is|me\s+llamo|mi\s+nombre\s+es)\b)\s+([^,;|]+)",
            message,
            re.I,
        )
        if match_name:
            labelled_name = _extract_name(match_name.group(1))

    segments = [segment.strip() for segment in re.split(r"[;,|]", message) if segment.strip()]
    # For a compact first answer, the documented field order is deterministic
    # and makes "Ana López, sí, Madrid centro, ..." a real multi-field patch.
    if len(segments) >= 3 and not any(
        value is not None
        for value in (
            labelled_name,
            labelled_license,
            labelled_location,
            labelled_availability,
            labelled_schedule,
            labelled_experience,
            labelled_start,
        )
    ):
        ordered = iter(segments)
        first = next(ordered, "")
        if state.current_field is ScreeningField.FULL_NAME or re.search(
            r"[A-Za-zÀ-ÿ].*\s+[A-Za-zÀ-ÿ]", first
        ):
            labelled_name = _extract_name(first)
        second = next(ordered, "")
        if _yes_no(second) is not None:
            labelled_license = second
        third = next(ordered, "")
        labelled_location = third
        fourth = next(ordered, "")
        if fourth:
            labelled_availability = fourth
        fifth = next(ordered, "")
        if fifth:
            labelled_schedule = fifth
        sixth = next(ordered, "")
        if sixth:
            labelled_experience = sixth
        seventh = next(ordered, "")
        if seventh:
            labelled_start = seventh
        eighth = next(ordered, "")
        if eighth and _yes_no(eighth) is not None:
            common["final_confirmation"] = _yes_no(eighth)

    if labelled_name is not None and labelled_name:
        fields["full_name"] = ExtractedValue(
            value=labelled_name, provided=True, correction=correction, evidence=evidence
        )
    if labelled_license is not None:
        fields["drivers_license"] = ExtractedValue(
            value=_yes_no(labelled_license),
            provided=_yes_no(labelled_license) is not None,
            correction=correction,
            evidence=evidence,
        )
    if labelled_location is not None and labelled_location:
        fields["location"] = ExtractedLocation(
            raw_value=labelled_location,
            provided=True,
            correction=correction,
            evidence=evidence,
        )
    if labelled_availability is not None:
        availability = _availability_value(labelled_availability)
        fields["availability"] = ExtractedValue(
            value=availability,
            provided=bool(availability),
            correction=correction,
            evidence=evidence,
        )
    if labelled_schedule is not None:
        schedule = _schedule_value(labelled_schedule)
        fields["preferred_schedule"] = ExtractedValue(
            value=schedule, provided=schedule is not None, correction=correction, evidence=evidence
        )
    if labelled_experience is not None:
        experience = _experience_value(labelled_experience)
        experience.correction = correction
        fields["delivery_experience"] = experience
    if labelled_start is not None:
        fields["start_availability"] = _start_value(labelled_start)
        fields["start_availability"].correction = correction

    # If no labelled/compact fields were found, interpret the answer for the
    # field requested by the deterministic state machine.
    if not fields:
        field = state.current_field
        if field is None:
            if yes_no is not None:
                common.update(final_confirmation=yes_no, confirmation=yes_no)
            return TurnInterpretation(**common)
        if field.value == "full_name":
            fields["full_name"] = ExtractedValue(
                value=_extract_name(message),
                provided=True,
                correction=correction,
                evidence=evidence,
            )
        elif field.value == "drivers_license":
            fields["drivers_license"] = ExtractedValue(
                value=yes_no, provided=yes_no is not None, correction=correction, evidence=evidence
            )
        elif field.value == "location":
            fields["location"] = ExtractedLocation(
                raw_value=message, provided=True, correction=correction, evidence=evidence
            )
        elif field.value == "availability":
            availability = _availability_value(message)
            fields["availability"] = ExtractedValue(
                value=availability,
                provided=bool(availability),
                correction=correction,
                evidence=evidence,
            )
        elif field.value == "preferred_schedule":
            schedule = _schedule_value(message)
            fields["preferred_schedule"] = ExtractedValue(
                value=schedule,
                provided=schedule is not None,
                correction=correction,
                evidence=evidence,
            )
        elif field.value == "delivery_experience":
            fields["delivery_experience"] = _experience_value(message)
            fields["delivery_experience"].correction = correction
        else:
            fields["start_availability"] = _start_value(message)
            fields["start_availability"].correction = correction

    common.update(fields)
    # Standalone yes/no at the final review stage is the only fallback final
    # confirmation.  A licence answer in a multi-field message is not consent.
    if state.current_field is None and len(segments) <= 1 and yes_no is not None:
        common.update(final_confirmation=yes_no, confirmation=yes_no)
    return TurnInterpretation(**common)


def _controller() -> ConversationController:
    matcher = ServiceAreaMatcher.from_file(_ROOT / "data" / "service_areas" / "service_areas.json")
    faq = FAQCatalog.from_file(_ROOT / "data" / "faq" / "faq.json")
    return ConversationController(ScreeningEngine(), matcher, faq_catalog=faq)


def _canonical_fields(state: ScreeningState) -> dict[str, Any]:
    """Return a compact, JSON-safe view of trusted state for eval assertions."""

    return {
        "preferred_language": state.preferred_language.value,
        "full_name": state.full_name.value if state.full_name else None,
        "drivers_license": state.drivers_license.value if state.drivers_license else None,
        "location": {
            "raw_value": state.location.raw_value,
            "normalized_value": state.location.normalized_value,
            "service_area_id": state.location.service_area_id,
            "matched_name": state.location.matched_name,
            "match_status": state.location.match_status.value,
            "confirmed": state.location.confirmed,
        },
        "availability": (
            [item.value for item in state.availability.value] if state.availability else None
        ),
        "preferred_schedule": (
            state.preferred_schedule.value.value if state.preferred_schedule else None
        ),
        "delivery_experience": (
            {
                "years": state.delivery_experience.years,
                "platforms": list(state.delivery_experience.platforms),
            }
            if state.delivery_experience
            else None
        ),
        "start_availability": (
            {
                "raw_value": state.start_availability.raw_value,
                "date": (
                    state.start_availability.date.isoformat()
                    if state.start_availability.date
                    else None
                ),
                "precision": state.start_availability.precision.value,
            }
            if state.start_availability
            else None
        ),
        "candidate_confirmed": state.candidate_confirmed,
        "current_field": state.current_field.value if state.current_field else None,
        "pending_confirmation": (
            {
                "field": state.pending_confirmation.field.value,
                "reason": state.pending_confirmation.reason,
            }
            if state.pending_confirmation
            else None
        ),
    }


def _value_matches(actual: Any, expected: Any) -> bool:
    """Compare expected JSON fragments while allowing partial nested objects."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        expected_mapping = cast(Mapping[str, Any], expected)
        actual_mapping = cast(Mapping[str, Any], actual)
        return all(
            key in actual_mapping and _value_matches(actual_mapping[key], value)
            for key, value in expected_mapping.items()
        )
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)):
        expected_sequence = cast(Sequence[Any], expected)
        return actual == list(expected_sequence) or actual == tuple(expected_sequence)
    if hasattr(actual, "value") and not isinstance(actual, (str, bytes, int, float, bool)):
        actual = actual.value
    return actual == expected


def _fields_match(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    for path, value in expected.items():
        if "." not in path:
            if path not in actual or not _value_matches(actual[path], value):
                return False
            continue
        current: Any = actual
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False
            current = cast(Mapping[str, Any], current)[part]
        if not _value_matches(current, value):
            return False
    return True


def _expected_reason_codes(case: EvalCase, scenario: EvalScenario) -> tuple[str, ...]:
    if case.expected_reason_codes:
        return case.expected_reason_codes
    value = scenario.expected.get("reason_codes", ())
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        values = cast(Sequence[Any], value)
        return tuple(str(item) for item in values if str(item).strip())
    return ()


def _expected_fields(case: EvalCase, scenario: EvalScenario) -> Mapping[str, Any]:
    if case.expected_fields:
        return case.expected_fields
    value = scenario.expected.get("canonical_fields", scenario.expected.get("fields", {}))
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _expected_language(case: EvalCase, scenario: EvalScenario) -> Language:
    if case.expected_language is not None:
        return case.expected_language
    value = scenario.expected.get("language", scenario.language.value)
    try:
        return Language(str(value))
    except ValueError:
        return scenario.language


def _expected_language_trace(case: EvalCase, scenario: EvalScenario) -> tuple[Language, ...]:
    if case.expected_language_trace:
        return case.expected_language_trace
    value = scenario.expected.get("language_trace", ())
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    result: list[Language] = []
    values = cast(Sequence[Any], value)
    for item in values:
        try:
            result.append(Language(str(item)))
        except ValueError:
            continue
    return tuple(result)


def _check_result(
    case: EvalCase,
    scenario: EvalScenario,
    *,
    status: ScreeningStatus,
    turns: int,
    security_event: bool,
    retryable: bool,
    reason_codes: Iterable[str] = (),
    error: str | None = None,
    state: ScreeningState | None = None,
    language_trace: Sequence[Language] = (),
    faq_answers: Sequence[str] = (),
    correction_fields: Sequence[str] = (),
    correction_confirmed: bool = False,
) -> EvalResult:
    expected_security = scenario.expected.get("security_event", case.security_event)
    expected_retryable = scenario.expected.get("retryable", case.retryable)
    actual_reason_codes = tuple(reason_codes)
    expected_reasons = _expected_reason_codes(case, scenario)
    expected_fields = _expected_fields(case, scenario)
    actual_fields = _canonical_fields(state) if state is not None else {}
    expected_lang = _expected_language(case, scenario)
    actual_language = state.preferred_language if state is not None else scenario.language
    expected_trace = _expected_language_trace(case, scenario)
    actual_trace = tuple(language_trace)
    answers = tuple(faq_answers)
    expected_faq_answered = case.expected_faq_answered
    if expected_faq_answered is None:
        scenario_faq = scenario.expected.get("faq_answered")
        expected_faq_answered = scenario_faq if isinstance(scenario_faq, bool) else None
    expected_faq_contains = case.expected_faq_contains
    if not expected_faq_contains:
        faq_contains_value = scenario.expected.get("faq_contains", ())
        if isinstance(faq_contains_value, str):
            expected_faq_contains = (faq_contains_value,)
        elif isinstance(faq_contains_value, Sequence):
            expected_faq_contains = tuple(
                str(item) for item in cast(Sequence[Any], faq_contains_value)
            )
    faq_match = (expected_faq_answered is None or bool(answers) == expected_faq_answered) and all(
        any(fragment.casefold() in answer.casefold() for answer in answers)
        for fragment in expected_faq_contains
    )
    expected_correction_field = case.expected_correction_field
    expected_correction_confirmed = case.expected_correction_confirmed
    correction_value = scenario.expected.get("correction", {})
    if isinstance(correction_value, Mapping):
        correction_mapping = cast(Mapping[str, Any], correction_value)
        if expected_correction_field is None and correction_mapping.get("field") is not None:
            expected_correction_field = str(correction_mapping["field"])
        if expected_correction_confirmed is None and isinstance(
            correction_mapping.get("confirmed"), bool
        ):
            expected_correction_confirmed = cast(bool, correction_mapping["confirmed"])
    correction_match: bool = (
        expected_correction_field is None or expected_correction_field in set(correction_fields)
    ) and (
        expected_correction_confirmed is None
        or correction_confirmed == expected_correction_confirmed
    )
    expected_error = case.expected_error
    if expected_error is None:
        scenario_error = scenario.expected.get("error_type", scenario.expected.get("error"))
        expected_error = str(scenario_error) if scenario_error else None
    passed: bool = (
        status is case.expected_status
        and turns <= case.max_turns
        and security_event == bool(expected_security)
        and retryable == bool(expected_retryable)
        and (not expected_reasons or set(expected_reasons).issubset(actual_reason_codes))
        and _fields_match(actual_fields, expected_fields)
        and actual_language is expected_lang
        and (not expected_trace or actual_trace == expected_trace)
        and faq_match
        and correction_match
        and (expected_error is None or error == expected_error)
    )
    return EvalResult(
        id=case.id,
        passed=passed,
        expected_status=case.expected_status,
        actual_status=status,
        turns=turns,
        security_event=security_event,
        retryable=retryable,
        error=error,
        reason_codes=actual_reason_codes,
        expected_reason_codes=expected_reasons,
        canonical_fields=actual_fields,
        expected_fields=expected_fields,
        language=actual_language,
        expected_language=expected_lang,
        language_trace=actual_trace,
        expected_language_trace=expected_trace,
        faq_answered=bool(answers),
        faq_answers=answers,
        expected_faq_answered=expected_faq_answered,
        correction_fields=tuple(correction_fields),
        correction_confirmed=correction_confirmed,
        expected_correction_field=expected_correction_field,
        expected_correction_confirmed=expected_correction_confirmed,
    )


def _correction_fields(interpretation: TurnInterpretation) -> tuple[str, ...]:
    result: list[str] = []
    for field_name in (
        "full_name",
        "drivers_license",
        "location",
        "availability",
        "preferred_schedule",
        "delivery_experience",
        "start_availability",
    ):
        value = getattr(interpretation, field_name)
        if value is not None and getattr(value, "correction", False):
            result.append(field_name)
    return tuple(result)


def _faq_answers_for(
    interpretation: TurnInterpretation,
    language: Language,
    faq_catalog: FAQCatalog,
) -> tuple[str, ...]:
    answers: list[str] = []
    for question in interpretation.candidate_questions:
        answer = faq_catalog.answer(question, language)
        if answer:
            answers.append(answer)
    return tuple(answers)


def run_deterministic(
    cases: Sequence[EvalCase] | None = None,
    scenarios: Mapping[str, EvalScenario] | None = None,
) -> EvalReport:
    """Run fixture cases without a provider, database, or network call."""

    selected_cases = tuple(cases or load_eval_cases())
    selected_scenarios = scenarios or load_scenarios()
    controller = _controller()
    faq_catalog = FAQCatalog.from_file(_ROOT / "data" / "faq" / "faq.json")
    results: list[EvalResult] = []
    for case in selected_cases:
        scenario = selected_scenarios.get(case.scenario)
        if scenario is None:
            results.append(
                EvalResult(
                    case.id,
                    False,
                    case.expected_status,
                    ScreeningStatus.IN_PROGRESS,
                    0,
                    error="scenario_not_found",
                )
            )
            continue
        state = ScreeningState.empty(scenario.language)
        last_status = ScreeningStatus.IN_PROGRESS
        security_event = False
        retryable = False
        error: str | None = None
        reason_codes: tuple[str, ...] = ()
        turns = 0
        language_trace: list[Language] = [state.preferred_language]
        faq_answers: list[str] = []
        correction_fields: list[str] = []
        correction_confirmed = False
        for message in scenario.turns[: case.max_turns]:
            turns += 1
            try:
                interpretation = _deterministic_interpretation(message, state)
                pending_before = state.pending_confirmation
                correction_fields.extend(_correction_fields(interpretation))
                outcome = controller.process(
                    state,
                    interpretation,
                    now=datetime.now(UTC),
                    message_id=f"eval:{case.id}:{turns}",
                )
                state = outcome.state
                last_status = outcome.decision.status
                security_event = security_event or outcome.security_event
                reason_codes = tuple(outcome.decision.reason_codes)
                faq_answers.extend(
                    _faq_answers_for(interpretation, outcome.state.preferred_language, faq_catalog)
                )
                if (
                    pending_before is not None
                    and pending_before.reason == "candidate_correction"
                    and interpretation.confirmation is True
                    and pending_before.field in outcome.changed_fields
                    and outcome.state.pending_confirmation is None
                ):
                    correction_confirmed = True
                if not language_trace or outcome.state.preferred_language is not language_trace[-1]:
                    language_trace.append(outcome.state.preferred_language)
                if outcome.opt_out or last_status in {
                    ScreeningStatus.QUALIFIED,
                    ScreeningStatus.DISQUALIFIED,
                    ScreeningStatus.ABANDONED,
                }:
                    break
            except Exception as exc:  # fixture errors are reported per case
                error = type(exc).__name__
                retryable = message in {"__provider_failure__", "__malformed_output__"}
                retained_decision = controller.screening_engine.evaluate(state)
                last_status = retained_decision.status
                reason_codes = tuple(retained_decision.reason_codes)
                break
        results.append(
            _check_result(
                case,
                scenario,
                status=last_status,
                turns=turns,
                security_event=security_event,
                retryable=retryable,
                reason_codes=reason_codes,
                error=error,
                state=state,
                language_trace=language_trace,
                faq_answers=faq_answers,
                correction_fields=correction_fields,
                correction_confirmed=correction_confirmed,
            )
        )
    return EvalReport("deterministic", tuple(results))


async def run_live(
    cases: Sequence[EvalCase] | None = None,
    scenarios: Mapping[str, EvalScenario] | None = None,
    *,
    settings: Settings | None = None,
    interpreter: AsyncInterpreter | None = None,
) -> EvalReport:
    """Run the same cases through a supplied/live typed interpreter."""

    selected_cases = tuple(cases or load_eval_cases())
    selected_scenarios = scenarios or load_scenarios()
    runtime_settings = settings or Settings()
    active_interpreter = interpreter or PydanticAIInterpreter(runtime_settings)
    controller = _controller()
    faq_catalog = FAQCatalog.from_file(_ROOT / "data" / "faq" / "faq.json")
    results: list[EvalResult] = []
    for case in selected_cases:
        scenario = selected_scenarios.get(case.scenario)
        if scenario is None:
            results.append(
                EvalResult(
                    case.id,
                    False,
                    case.expected_status,
                    ScreeningStatus.IN_PROGRESS,
                    0,
                    error="scenario_not_found",
                )
            )
            continue
        state = ScreeningState.empty(scenario.language)
        status = ScreeningStatus.IN_PROGRESS
        security_event = False
        retryable = False
        error: str | None = None
        reason_codes: tuple[str, ...] = ()
        turns = 0
        language_trace: list[Language] = [state.preferred_language]
        faq_answers: list[str] = []
        correction_fields: list[str] = []
        correction_confirmed = False
        for message in scenario.turns[: case.max_turns]:
            turns += 1
            try:
                dependencies = InterpreterDependencies(
                    state=state,
                    pending_field=state.current_field,
                    language=state.preferred_language,
                    now=datetime.now(UTC),
                    local_date=date.today().isoformat(),
                )
                interpreted = await active_interpreter.interpret(message, dependencies)
                pending_before = state.pending_confirmation
                correction_fields.extend(_correction_fields(interpreted.interpretation))
                outcome = controller.process(
                    state,
                    interpreted.interpretation,
                    now=datetime.now(UTC),
                    message_id=f"eval:{case.id}:{turns}",
                )
                state = outcome.state
                status = outcome.decision.status
                security_event = security_event or outcome.security_event
                reason_codes = tuple(outcome.decision.reason_codes)
                faq_answers.extend(
                    _faq_answers_for(
                        interpreted.interpretation,
                        outcome.state.preferred_language,
                        faq_catalog,
                    )
                )
                if (
                    pending_before is not None
                    and pending_before.reason == "candidate_correction"
                    and interpreted.interpretation.confirmation is True
                    and pending_before.field in outcome.changed_fields
                    and outcome.state.pending_confirmation is None
                ):
                    correction_confirmed = True
                if not language_trace or outcome.state.preferred_language is not language_trace[-1]:
                    language_trace.append(outcome.state.preferred_language)
                if outcome.opt_out or status in {
                    ScreeningStatus.QUALIFIED,
                    ScreeningStatus.DISQUALIFIED,
                    ScreeningStatus.ABANDONED,
                }:
                    break
            except Exception as exc:
                error = type(exc).__name__
                retryable = True
                retained_decision = controller.screening_engine.evaluate(state)
                status = retained_decision.status
                reason_codes = tuple(retained_decision.reason_codes)
                break
        results.append(
            _check_result(
                case,
                scenario,
                status=status,
                turns=turns,
                security_event=security_event,
                retryable=retryable,
                reason_codes=reason_codes,
                error=error,
                state=state,
                language_trace=language_trace,
                faq_answers=faq_answers,
                correction_fields=correction_fields,
                correction_confirmed=correction_confirmed,
            )
        )
    return EvalReport("live", tuple(results))


__all__ = [
    "DEFAULT_EVALS_PATH",
    "DEFAULT_SCENARIOS_PATH",
    "EvalCase",
    "EvalReport",
    "EvalResult",
    "EvalScenario",
    "load_eval_cases",
    "load_scenarios",
    "run_deterministic",
    "run_live",
]
