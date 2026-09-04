"""Conservative, provider-independent interpretation shortcuts.

The language model remains responsible for open-ended natural-language
understanding. This module handles only inputs whose meaning is sufficiently
closed and auditable to process without a provider call: control replies,
explicit language changes, exact catalogue locations, obvious questions, and
labelled multi-field answers. It returns the same typed patch consumed by the
normal workflow.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, cast

from candidate_screening.ai.interpreter import InterpreterResult
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
    Language,
    LocationMatchStatus,
    SchedulePreference,
    ScreeningField,
)
from candidate_screening.domain.models import ScreeningState
from candidate_screening.domain.service_areas import (
    ServiceArea,
    ServiceAreaMatch,
    ServiceAreaMatcher,
    normalize_location,
)

_YES_VALUES = frozenset({"yes", "y", "sí", "si", "true", "correct", "correcto", "vale"})
_NO_VALUES = frozenset({"no", "n", "false", "nope"})
_LICENSE_VALUES: dict[str, bool] = {
    **{value: True for value in _YES_VALUES},
    **{value: False for value in _NO_VALUES},
}
_DISCLOSURE_ACKNOWLEDGEMENTS = frozenset(
    {
        "yes",
        "y",
        "sí",
        "si",
        "yes please",
        "sí por favor",
        "si por favor",
        "sure",
        "okay",
        "ok",
        "vale",
        "de acuerdo",
        "adelante",
        "go on",
        "go ahead",
        "continue",
        "let's continue",
        "lets continue",
        "start",
        "start please",
        "empecemos",
        "empecemos por favor",
        "empezamos",
        "comencemos",
        "continuar",
        "continuemos",
    }
)
_NAME_PREFIX = re.compile(
    r"^(?:my\s+full\s+name\s+is|my\s+name\s+is|name\s+is|i\s+am|"
    r"i['’]?m|me\s+llamo|mi\s+nombre\s+es)\s+(.+?)\s*[.!?]?$",
    re.IGNORECASE,
)
_NAME_TOKEN = re.compile(r"^[^\W\d_]+(?:[-'’][^\W\d_]+)*$", re.UNICODE)
_NON_NAME_TOKENS = frozenset(
    {
        "adelante",
        "and",
        "area",
        "at",
        "available",
        "años",
        "año",
        "begin",
        "can",
        "center",
        "city",
        "ciudad",
        "completo",
        "comencemos",
        "continue",
        "continuar",
        "continuemos",
        "delivery",
        "empecemos",
        "empezamos",
        "es",
        "experience",
        "favor",
        "fines",
        "from",
        "full",
        "gracias",
        "go",
        "have",
        "has",
        "home",
        "interesado",
        "interested",
        "is",
        "live",
        "lives",
        "llamo",
        "licencia",
        "license",
        "mi",
        "my",
        "name",
        "no",
        "on",
        "ok",
        "okay",
        "part",
        "parcial",
        "please",
        "por",
        "reparto",
        "schedule",
        "si",
        "sí",
        "start",
        "sure",
        "tengo",
        "tiene",
        "trabajo",
        "valid",
        "vigente",
        "weekends",
        "years",
        "y",
        "zona",
        "yes",
        "central",
        "centro",
        "east",
        "este",
        "north",
        "norte",
        "south",
        "sur",
        "west",
        "oeste",
        "zone",
    }
)
_QUESTION_START = re.compile(
    r"^(?:what|how|why|which|when|where|"
    r"qué|que|cómo|como|cuál|cual|dónde|donde|cuándo|cuando)\b",
    re.IGNORECASE,
)
_ENGLISH_AUXILIARY_QUESTION = re.compile(
    r"^(?:can|could|would|will|do|does|did|is|are|am|should|may|might)\s+"
    r"(?:i|we|you|he|she|they|it|this|that|these|those|there|the|a|an)\b",
    re.IGNORECASE,
)
_OFF_TOPIC_WORDS = frozenset(
    {"joke", "weather", "favorite", "politics", "hobby", "meaning", "chiste"}
)


def _normalize(message: str) -> str:
    normalized = re.sub(r"[.,!?;:¡¿]+", " ", message.casefold())
    return " ".join(normalized.split())


def _words(message: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[\wÀ-ÿ]+", message)}


def _yes_no(message: str) -> bool | None:
    words = _words(message)
    has_yes = bool(words & _YES_VALUES)
    has_no = bool(words & _NO_VALUES)
    if has_yes == has_no:
        return None
    return has_yes


def _explicit_language(message: str) -> Language | None:
    lowered = message.casefold()
    if re.search(r"\b(?:english|inglés|ingles|in english|en inglés|en ingles)\b", lowered):
        return Language.EN
    if re.search(r"\b(?:spanish|español|espanol|in spanish|en español|en espanol)\b", lowered):
        return Language.ES
    return None


def _language_for_message(message: str, state: ScreeningState) -> Language:
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
        "asap",
        "licence",
        "license",
        "name",
        "location",
        "schedule",
        "experience",
        "when",
        "where",
        "what",
        "how",
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
        "cuándo",
        "cuando",
        "dónde",
        "donde",
        "qué",
        "que",
    }
    if len(english) > len(spanish):
        return Language.EN
    if len(spanish) > len(english):
        return Language.ES
    return state.preferred_language


def _base(message: str, state: ScreeningState) -> dict[str, Any]:
    return {
        "detected_language": _language_for_message(message, state),
        "language_confidence": 0.9,
        "intent": TurnIntent.ANSWER,
    }


def _extract_name(value: str) -> str:
    value = re.sub(
        r"^\s*(?:my\s+full\s+name\s+is|my\s+name\s+is|name\s+is|i\s+am|i['’]?m|"
        r"me\s+llamo|mi\s+nombre\s+es)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip(' \t,.;:|-"')


def _simple_name(value: str) -> str | None:
    candidate = _extract_name(value)
    words = candidate.split()
    if not 2 <= len(words) <= 6:
        return None
    if any(not _NAME_TOKEN.fullmatch(word) for word in words):
        return None
    if any(word.casefold() in _NON_NAME_TOKENS for word in words):
        return None
    return candidate[:200]


def _bare_name(value: str) -> str | None:
    """Accept only a title-cased, alphabetic name-shaped answer."""

    candidate = " ".join(value.split()).strip(" .,!?;:¡¿")
    words = candidate.split()
    if not 2 <= len(words) <= 6:
        return None
    if any(not word[0].isupper() for word in words):
        return None
    return _simple_name(candidate)


def _label_value(message: str, labels: str) -> str | None:
    match = re.search(
        rf"(?:^|[;,|])\s*(?:{labels})\s*(?:is|es|=|:)\s*([^,;|]+)",
        message,
        re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            rf"\b(?:{labels})\s*(?:is|es|=|:)\s*([^,;|]+)",
            message,
            re.IGNORECASE,
        )
    return _extract_name(match.group(1)) if match else None


def _availability_value(value: str) -> list[AvailabilityType]:
    words = _words(value)
    result: list[AvailabilityType] = []
    if "full" in words or "completo" in words or "completa" in words:
        result.append(AvailabilityType.FULL_TIME)
    if "part" in words or "parcial" in words:
        result.append(AvailabilityType.PART_TIME)
    if "weekend" in words or "weekends" in words or "fin" in words or "semana" in words:
        result.append(AvailabilityType.WEEKENDS)
    return list(dict.fromkeys(result))


def _schedule_value(value: str) -> SchedulePreference | None:
    words = _words(value)
    if "morning" in words or "mañana" in words:
        return SchedulePreference.MORNING
    if "afternoon" in words or "tarde" in words:
        return SchedulePreference.AFTERNOON
    if "evening" in words or "night" in words or "noche" in words:
        return SchedulePreference.EVENING
    if "flexible" in words:
        return SchedulePreference.FLEXIBLE
    return None


def _experience_value(value: str) -> ExtractedDeliveryExperience:
    match = re.search(r"(?:^|\D)(\d+(?:[.,]\d+)?)\s*(?:years?|años?)?", value, re.IGNORECASE)
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
    elif "week" in words or "semana" in words:
        precision = "week"
    elif "month" in words or "mes" in words:
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
            r"\b(?:actually|instead|correction|correct(?:ion)?|change|update|"
            r"me equivoqué|quise decir|en realidad)\b",
            message,
            re.IGNORECASE,
        )
    )


def _question_or_off_topic(message: str, state: ScreeningState) -> TurnInterpretation | None:
    lowered = message.casefold().strip().lstrip("¿¡ \t")
    words = _words(message)
    if _looks_like_question(message, lowered=lowered):
        common = _base(message, state)
        common.update(
            intent=TurnIntent.QUESTION,
            response_requested=True,
            candidate_questions=[message[:500]],
        )
        return TurnInterpretation(**common)
    if words & _OFF_TOPIC_WORDS:
        common = _base(message, state)
        common.update(intent=TurnIntent.OFF_TOPIC, response_requested=True)
        return TurnInterpretation(**common)
    return None


def _looks_like_question(message: str, *, lowered: str | None = None) -> bool:
    """Recognize questions without treating ability statements as questions.

    A question mark (including the Spanish opening mark) is authoritative.
    Without punctuation, only unambiguous interrogative words or an English
    auxiliary followed by a subject/determiner qualify.  Forms such as
    ``Puedo trabajar ...`` and ``Can work ...`` remain model-owned answers.
    """

    normalized = lowered if lowered is not None else message.casefold().strip().lstrip("¿¡ \t")
    return bool(
        "?" in message
        or "¿" in message
        or _QUESTION_START.match(normalized)
        or _ENGLISH_AUXILIARY_QUESTION.match(normalized)
    )


def _phrase_in_text(text: str, phrase: str) -> bool:
    """Return whether a normalized phrase occurs on token boundaries."""

    if not text or not phrase:
        return False
    return text == phrase or f" {phrase} " in f" {text} "


def _catalog_city_mentions(text: str, service_area_matcher: ServiceAreaMatcher) -> dict[str, str]:
    """Return canonical cities explicitly named in a candidate message.

    This intentionally uses only catalogue city names and aliases.  It is not
    a general-purpose location NER pass, which would make a model-free
    recovery path too eager to interpret ordinary prose.
    """

    normalized = normalize_location(text)
    mentions: dict[str, str] = {}
    for area in service_area_matcher.catalog.areas:
        canonical_key = normalize_location(area.city)
        if any(
            _phrase_in_text(normalized, normalize_location(city_value))
            for city_value in (area.city, *area.city_aliases)
        ):
            mentions[canonical_key] = area.city
    return mentions


def _catalog_area_mentions(
    text: str, service_area_matcher: ServiceAreaMatcher
) -> dict[str, tuple[ServiceArea, str]]:
    """Return exact catalogue aliases occurring in a candidate message."""

    normalized = normalize_location(text)
    mentions: dict[str, tuple[ServiceArea, str]] = {}
    for area in service_area_matcher.catalog.areas:
        values = (area.display_name, f"{area.city} {area.zone}", *area.aliases)
        for value in values:
            alias = normalize_location(value)
            if alias and _phrase_in_text(normalized, alias):
                # Prefer the longest matching phrase when an area has both a
                # short and a verbose alias in the same message.
                previous = mentions.get(area.id)
                if previous is None or len(alias) > len(normalize_location(previous[1])):
                    mentions[area.id] = (area, value)
    return mentions


def _location_match_is_safe(
    match: ServiceAreaMatch, candidate: str, service_area_matcher: ServiceAreaMatcher
) -> bool:
    """Accept only catalogue-exact areas or a bare known-city offer.

    Fuzzy suggestions and unsupported locations are deliberately excluded.
    A known city is safe to recover because reconciliation represents it as a
    pending explicit area-scope confirmation; it never selects an area here.
    """

    if match.status is LocationMatchStatus.EXACT and match.area is not None:
        return True
    if match.status is not LocationMatchStatus.AMBIGUOUS or match.area is not None:
        return False
    normalized = normalize_location(candidate)
    city_mentions = _catalog_city_mentions(candidate, service_area_matcher)
    return len(city_mentions) == 1 and any(
        normalized == city_key
        or any(
            normalized == normalize_location(alias)
            for area in service_area_matcher.catalog.areas
            if normalize_location(area.city) == city_key
            for alias in area.city_aliases
        )
        for city_key in city_mentions
    )


def _location_text_is_grounded(
    candidate: str,
    message: str,
    patch: ExtractedLocation | None,
    service_area_matcher: ServiceAreaMatcher,
) -> bool:
    """Check that model-provided location text is supported by user text."""

    source = normalize_location(message)
    candidate_normalized = normalize_location(candidate)
    if not source or not candidate_normalized:
        return False
    evidence = normalize_location(patch.evidence) if patch and patch.evidence else ""
    if evidence and not _phrase_in_text(source, evidence):
        return False
    source_tokens = set(source.split())
    candidate_tokens = set(candidate_normalized.split())
    # A provider commonly shortens ``The city center of Madrid`` to the
    # catalogue alias ``city center``.  Token containment handles that while
    # keeping unrelated model text out of the recovery path.
    if not (
        _phrase_in_text(source, candidate_normalized)
        or candidate_tokens <= source_tokens
        or (evidence and candidate_normalized == evidence)
    ):
        return False
    if patch is None or not patch.city:
        return True
    # Canonical city names can differ from the candidate's configured alias
    # (for example ``Ciudad de México`` / ``Mexico City``).  Require one of
    # the catalogue's canonical/alias spellings to occur in the source or its
    # evidence before using a structured city hint.
    city_text = f"{message} {patch.evidence}".strip()
    return bool(
        _catalog_city_mentions(city_text, service_area_matcher).get(normalize_location(patch.city))
    )


def _location_patch(
    base: ExtractedLocation | None,
    *,
    raw_value: str,
    city: str | None,
    zone: str | None,
    evidence: str,
    confidence: float,
) -> ExtractedLocation:
    """Build a provided location patch while preserving model provenance."""

    return ExtractedLocation(
        raw_value=raw_value[:300] or None,
        city=city,
        zone=zone,
        provided=True,
        ambiguous=False,
        correction=base.correction if base is not None else False,
        explicit_confirmation=base.explicit_confirmation if base is not None else False,
        evidence=(base.evidence if base is not None and base.evidence else evidence[:500]),
        # Once the closed catalogue matcher has established the result, its
        # confidence—not a provider's contradictory default—is authoritative.
        confidence=max(confidence, base.confidence if base is not None else 0.0),
    )


def _deterministic_location_interpretation(
    state: ScreeningState,
    message: str,
    *,
    service_area_matcher: ServiceAreaMatcher | None,
) -> InterpreterResult | None:
    """Interpret only a closed location answer before contacting the provider.

    This is intentionally narrower than :func:`recover_location_answer`,
    which also inspects a provider patch and exact aliases embedded in prose.
    Before a provider call there is no typed patch to ground, so only a
    complete catalogue alias, a bare known city, or a zone answer grounded by
    an existing city offer is eligible.  In particular, a fuzzy suggestion,
    unsupported city, ambiguous phrase, or natural-language sentence remains
    model-owned.
    """

    if service_area_matcher is None:
        return None

    pending = state.pending_confirmation
    location_active = state.current_field is ScreeningField.LOCATION or (
        pending is not None and pending.field is ScreeningField.LOCATION
    )
    if not location_active:
        return None
    # A pending correction or fuzzy suggestion must still be resolved through
    # the normal confirmation path.  The only pending location answer that is
    # safe to shortcut is a concrete zone for a trusted city-level offer.
    if pending is not None and (
        pending.field is not ScreeningField.LOCATION or pending.reason != "service_area_city"
    ):
        return None

    message_text = message.strip()
    if not message_text or _looks_like_question(message_text):
        return None

    direct_match = service_area_matcher.match(message_text)
    selected: tuple[ServiceAreaMatch, str] | None = None
    if _location_match_is_safe(direct_match, message_text, service_area_matcher):
        if direct_match.area is not None:
            selected = (direct_match, "preprovider_exact_alias")
        elif pending is None:
            # A bare known city is represented as a city-level offer by
            # reconciliation.  It never selects the first catalogue area.
            selected = (direct_match, "preprovider_known_city")

    if pending is not None:
        proposed = pending.proposed_value
        proposed_mapping = cast(dict[str, Any], proposed) if isinstance(proposed, dict) else {}
        allowed_ids = {
            str(area_id)
            for area_id in proposed_mapping.get("service_area_ids", [])
            if isinstance(area_id, str)
        }
        if selected is not None and (
            selected[0].area is None or selected[0].area.id not in allowed_ids
        ):
            # Repeating a city or naming an area outside the offered city is
            # not a trusted concrete answer to this confirmation.  Leave it
            # on the model path so reconciliation can preserve the pending
            # proposal without inventing an eligible area.
            selected = None
        if selected is None:
            city = state.location.city
            if city is None:
                proposed_city = proposed_mapping.get("city")
                city = proposed_city if isinstance(proposed_city, str) else None
            if city:
                contextual_match = service_area_matcher.match(message_text, city=city)
                if (
                    contextual_match.status is LocationMatchStatus.EXACT
                    and contextual_match.area is not None
                    and contextual_match.area.id in allowed_ids
                ):
                    selected = (contextual_match, "preprovider_pending_city_zone")

    if selected is None:
        return None

    match, reason = selected
    city = match.area.city if match.area is not None else match.city
    zone = match.area.zone if match.area is not None else None
    patch = _location_patch(
        None,
        raw_value=message_text,
        city=city,
        zone=zone,
        evidence=message_text,
        confidence=match.confidence,
    )
    return InterpreterResult(
        interpretation=TurnInterpretation(
            **_base(message_text, state),
            location=patch,
        ),
        usage={
            "requests": 0,
            "deterministic": True,
            "deterministic_reason": reason,
        },
    )


def recover_location_answer(
    state: ScreeningState,
    message: str,
    interpreted: InterpreterResult,
    *,
    service_area_matcher: ServiceAreaMatcher,
) -> InterpreterResult:
    """Recover an obvious location answer omitted by a valid model response.

    The provider is still responsible for open-ended extraction.  This narrow
    fallback runs only while location is active (or a location confirmation is
    pending), and only accepts an exact catalogue alias or a known city.  It
    never turns a fuzzy suggestion into an eligible area and never trusts a
    structured city/zone hint that is contradicted by the candidate message.
    """

    interpretation = interpreted.interpretation
    if interpretation.prompt_injection_detected or interpretation.sensitive_data_detected:
        return interpreted
    if (
        interpretation.intent
        in {
            TurnIntent.QUESTION,
            TurnIntent.OFF_TOPIC,
            TurnIntent.OPT_OUT,
            TurnIntent.PROMPT_INJECTION,
        }
        or interpretation.response_requested
    ):
        return interpreted
    pending = state.pending_confirmation
    location_active = state.current_field is ScreeningField.LOCATION or (
        pending is not None and pending.field is ScreeningField.LOCATION
    )
    if not location_active:
        return interpreted
    existing_patch = interpretation.location
    if existing_patch is not None and existing_patch.provided:
        return interpreted
    if existing_patch is not None and existing_patch.ambiguous:
        return interpreted

    message_text = message.strip()
    if not message_text:
        return interpreted
    if _looks_like_question(message_text):
        return interpreted

    selected: tuple[str, ServiceAreaMatch, str | None, str | None, str] | None = None

    # A direct answer is the strongest evidence and is also the safest way to
    # ensure a hallucinated structured city cannot override the raw message.
    direct_match = service_area_matcher.match(message_text)
    if _location_match_is_safe(direct_match, message_text, service_area_matcher):
        selected = (message_text, direct_match, direct_match.city, None, "message_exact")

    # Natural sentences may contain an exact alias (for example, ``I live in
    # Madrid Centro``).  Resolve that phrase only when exactly one catalogue
    # area/city is mentioned; multiple locations remain unresolved.
    if selected is None:
        area_mentions = _catalog_area_mentions(message_text, service_area_matcher)
        city_mentions = _catalog_city_mentions(message_text, service_area_matcher)
        if len(area_mentions) == 1:
            area = next(iter(area_mentions.values()))[0]
            if len(city_mentions) <= 1 and (
                not city_mentions or normalize_location(area.city) in city_mentions
            ):
                area_match = service_area_matcher.match(
                    message_text, city=area.city, zone=area.zone
                )
                if area_match.status is LocationMatchStatus.EXACT and area_match.area:
                    selected = (
                        message_text,
                        area_match,
                        area.city,
                        area.zone,
                        "message_catalog_alias",
                    )
        elif not area_mentions and len(city_mentions) == 1:
            city = next(iter(city_mentions.values()))
            city_match = service_area_matcher.match(city)
            if _location_match_is_safe(city_match, city, service_area_matcher):
                selected = (message_text, city_match, city, None, "message_known_city")

    # A provider may preserve a short raw phrase in the patch but forget the
    # ``provided`` bit.  Reuse it only when its evidence is grounded in the
    # current user message.  Pending city offers additionally provide trusted
    # city context for a short answer such as ``Centro``.
    if selected is None and existing_patch is not None:
        raw = _extract_location_raw(existing_patch)
        if raw and _location_text_is_grounded(
            raw, message_text, existing_patch, service_area_matcher
        ):
            patch_city = existing_patch.city
            if patch_city is None and pending is not None and pending.reason == "service_area_city":
                patch_city = state.location.city
            patch_match = service_area_matcher.match(
                raw,
                city=patch_city,
                zone=existing_patch.zone,
            )
            if _location_match_is_safe(patch_match, raw, service_area_matcher):
                selected = (
                    raw,
                    patch_match,
                    patch_match.city or patch_city,
                    existing_patch.zone,
                    "model_location_evidence",
                )

    # If the provider omitted the nested patch entirely, a short answer to a
    # known pending city can still be resolved with that trusted city context.
    if selected is None and pending is not None and pending.reason == "service_area_city":
        city = state.location.city
        if city:
            contextual_match = service_area_matcher.match(message_text, city=city)
            if contextual_match.status is LocationMatchStatus.EXACT and contextual_match.area:
                selected = (
                    message_text,
                    contextual_match,
                    contextual_match.city,
                    contextual_match.area.zone,
                    "pending_city_zone",
                )

    if selected is None:
        # Do not let reconciliation's legacy ``provided`` repair treat an
        # unsupported model-only patch as a fact.  If the current message did
        # not ground the patch, discard only this absent location field and
        # keep any other typed facts from the provider response.
        if existing_patch is not None and not existing_patch.provided:
            return replace(
                interpreted,
                interpretation=interpretation.model_copy(update={"location": None}),
            )
        return interpreted

    raw, match, city, zone, reason = selected
    # Preserve canonical city/zone hints from the deterministic match, not a
    # model-supplied value that may have been inconsistent with the source.
    if match.area is not None:
        city = match.area.city
        zone = match.area.zone
    recovered = _location_patch(
        existing_patch,
        raw_value=raw,
        city=city,
        zone=zone,
        evidence=message_text,
        confidence=match.confidence,
    )
    patched_interpretation = interpretation.model_copy(update={"location": recovered})
    usage = dict(interpreted.usage)
    usage.update(
        {
            "deterministic_fallback": True,
            "deterministic_fallback_field": ScreeningField.LOCATION.value,
            "deterministic_fallback_reason": reason,
        }
    )
    return replace(interpreted, interpretation=patched_interpretation, usage=usage)


def _extract_location_raw(patch: ExtractedLocation) -> str:
    return (
        patch.raw_value
        or " ".join(part for part in (patch.city, patch.zone) if part)
        or patch.evidence
    ).strip()


def _labelled_interpretation(message: str, state: ScreeningState) -> TurnInterpretation | None:
    """Parse at least two explicit labels; invalid values become clarifications."""

    correction = _correction_requested(message)
    evidence = message[:500]
    fields: dict[str, Any] = {}
    labelled_name = _label_value(message, r"full\s+name|name|nombre")
    labelled_license = _label_value(message, r"driver'?s?\s+licen[cs]e|licen[cs]e|licencia|carnet")
    labelled_location = _label_value(message, r"location|city|area|ubicación|ubicacion|ciudad|zona")
    labelled_availability = _label_value(message, r"availability|disponibilidad")
    labelled_schedule = _label_value(message, r"schedule|horario|turno")
    labelled_experience = _label_value(message, r"experience|experiencia")
    labelled_start = _label_value(message, r"start(?:\s+date)?|empezar|incorporación|incorporacion")
    labels_present = [
        labelled_name,
        labelled_license,
        labelled_location,
        labelled_availability,
        labelled_schedule,
        labelled_experience,
        labelled_start,
    ]
    minimum_labels = 1 if correction and labelled_name is not None else 2
    if sum(value is not None for value in labels_present) < minimum_labels:
        return None

    if labelled_name is not None:
        name = _simple_name(labelled_name)
        fields["full_name"] = ExtractedValue(
            value=name,
            provided=True,
            ambiguous=name is None,
            correction=correction,
            evidence=evidence,
        )
    if labelled_license is not None:
        license_value = _yes_no(labelled_license)
        fields["drivers_license"] = ExtractedValue(
            value=license_value,
            provided=True,
            ambiguous=license_value is None,
            correction=correction,
            evidence=evidence,
        )
    if labelled_location is not None:
        fields["location"] = ExtractedLocation(
            raw_value=labelled_location.strip() or None,
            provided=True,
            ambiguous=not bool(labelled_location.strip()),
            correction=correction,
            evidence=evidence,
        )
    if labelled_availability is not None:
        availability = _availability_value(labelled_availability)
        fields["availability"] = ExtractedValue(
            value=availability or None,
            provided=True,
            ambiguous=not bool(availability),
            correction=correction,
            evidence=evidence,
        )
    if labelled_schedule is not None:
        schedule = _schedule_value(labelled_schedule)
        fields["preferred_schedule"] = ExtractedValue(
            value=schedule,
            provided=True,
            ambiguous=schedule is None,
            correction=correction,
            evidence=evidence,
        )
    if labelled_experience is not None:
        experience = _experience_value(labelled_experience)
        experience.correction = correction
        if not experience.provided:
            experience.ambiguous = True
            experience.provided = True
        fields["delivery_experience"] = experience
    if labelled_start is not None:
        start = _start_value(labelled_start)
        start.correction = correction
        start.ambiguous = not start.provided
        start.provided = True
        fields["start_availability"] = start

    common = _base(message, state)
    common.update(intent=TurnIntent.CORRECTION if correction else TurnIntent.ANSWER, **fields)
    return TurnInterpretation(**common)


def interpret_deterministically(
    state: ScreeningState,
    message: str,
    *,
    service_area_matcher: ServiceAreaMatcher | None = None,
) -> InterpreterResult | None:
    """Return a safe typed interpretation, or None for model-owned prose."""

    normalized = _normalize(message)
    base = _base(message, state)
    if state.pending_confirmation is not None:
        confirmation = _yes_no(message)
        if confirmation is not None and normalized in _YES_VALUES | _NO_VALUES:
            return InterpreterResult(
                interpretation=TurnInterpretation(**base, confirmation=confirmation),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "confirmation",
                },
            )
        if (
            state.pending_confirmation.field is not ScreeningField.LOCATION
            or state.pending_confirmation.reason != "service_area_city"
        ):
            return None

    if state.current_field is ScreeningField.DRIVERS_LICENSE:
        license_answer = _LICENSE_VALUES.get(normalized)
        if license_answer is not None:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    drivers_license=ExtractedValue(
                        value=license_answer, provided=True, evidence=message[:500]
                    ),
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "license_control",
                },
            )

    if state.current_field is None:
        confirmation = _yes_no(message)
        if confirmation is not None and normalized in _YES_VALUES | _NO_VALUES:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    confirmation=confirmation,
                    final_confirmation=confirmation,
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "final_confirmation",
                },
            )

    if not state.disclosure_acknowledged and normalized in _DISCLOSURE_ACKNOWLEDGEMENTS:
        return InterpreterResult(
            interpretation=TurnInterpretation(**base, disclosure_acknowledged=True),
            usage={"requests": 0, "deterministic": True, "deterministic_reason": "disclosure"},
        )

    explicit = _explicit_language(message)
    if (
        explicit is not None
        and not re.search(
            r"\b(?:name|nombre|licen[cs]e|licencia|carnet|location|city|ciudad|"
            r"area|zona|availability|disponibilidad|schedule|horario|experience|"
            r"experiencia|start|empezar)\b",
            normalized,
        )
        and _yes_no(message) is None
    ):
        return InterpreterResult(
            interpretation=TurnInterpretation(
                detected_language=explicit,
                language_confidence=1.0,
                explicit_language=explicit,
            ),
            usage={"requests": 0, "deterministic": True, "deterministic_reason": "language_switch"},
        )

    question = _question_or_off_topic(message, state)
    if question is not None:
        return InterpreterResult(
            interpretation=question,
            usage={
                "requests": 0,
                "deterministic": True,
                "deterministic_reason": "question_or_off_topic",
            },
        )

    labelled = _labelled_interpretation(message, state)
    if labelled is not None:
        return InterpreterResult(
            interpretation=labelled,
            usage={"requests": 0, "deterministic": True, "deterministic_reason": "labelled_fields"},
        )

    location = _deterministic_location_interpretation(
        state,
        message,
        service_area_matcher=service_area_matcher,
    )
    if location is not None:
        return location

    if state.current_field is ScreeningField.FULL_NAME:
        name = _simple_name(message) if _NAME_PREFIX.match(message.strip()) else _bare_name(message)
        if name is not None:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    disclosure_acknowledged=True,
                    full_name=ExtractedValue(value=name, provided=True, evidence=message[:500]),
                ),
                usage={"requests": 0, "deterministic": True, "deterministic_reason": "name_prefix"},
            )

    return None


__all__ = ["interpret_deterministically"]
