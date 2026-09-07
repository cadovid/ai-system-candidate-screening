"""Conservative, provider-independent interpretation shortcuts.

The language model remains responsible for open-ended natural-language
understanding. This module handles only inputs whose meaning is sufficiently
closed and auditable to process without a provider call: exact control replies,
explicit language changes, exact catalogue locations, and an explicitly
prefixed name. Compound answers, corrections, questions, and other prose stay
on the semantic model path. It returns the same typed patch consumed by the
normal workflow.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Any, cast

from candidate_screening.ai.interpreter import InterpreterResult
from candidate_screening.ai.schemas import (
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

_YES_VALUES = frozenset({"yes", "y", "si", "true", "correct", "correcto", "vale"})
_NO_VALUES = frozenset({"no", "n", "false", "nope"})
_LICENSE_VALUES: dict[str, bool] = {
    **{value: True for value in _YES_VALUES},
    **{value: False for value in _NO_VALUES},
}
_DISCLOSURE_ACKNOWLEDGEMENTS = frozenset(
    {
        "yes",
        "y",
        "si",
        "sure",
        "okay",
        "ok",
        "vale",
        "adelante",
        "go ahead",
        "let s go",
        "lets go",
        "continue",
        "start",
        "empecemos",
        "empezamos",
        "comencemos",
        "continuar",
    }
)

_EXACT_AVAILABILITY: dict[str, AvailabilityType] = {
    "full time": AvailabilityType.FULL_TIME,
    "part time": AvailabilityType.PART_TIME,
    "weekend": AvailabilityType.WEEKENDS,
    "weekends": AvailabilityType.WEEKENDS,
    "tiempo completo": AvailabilityType.FULL_TIME,
    "jornada completa": AvailabilityType.FULL_TIME,
    "tiempo parcial": AvailabilityType.PART_TIME,
    "media jornada": AvailabilityType.PART_TIME,
    "fin de semana": AvailabilityType.WEEKENDS,
    "fines de semana": AvailabilityType.WEEKENDS,
}
_EXACT_SCHEDULE: dict[str, SchedulePreference] = {
    "morning": SchedulePreference.MORNING,
    "afternoon": SchedulePreference.AFTERNOON,
    "evening": SchedulePreference.EVENING,
    "flexible": SchedulePreference.FLEXIBLE,
    "manana": SchedulePreference.MORNING,
    "tarde": SchedulePreference.AFTERNOON,
    "noche": SchedulePreference.EVENING,
}
_EXACT_OPT_OUT = frozenset({"stop", "quit", "cancel", "parar", "salir", "terminar"})
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


def _normalize(message: str) -> str:
    """Normalize only syntax that cannot change a closed control reply.

    Control fast paths compare the resulting whole phrase, never individual
    words.  Folding accents and Unicode hyphens makes ``sí``, ``si!`` and
    ``go-ahead`` equivalent while keeping natural sentences outside the
    closed vocabulary.
    """

    decomposed = unicodedata.normalize("NFKD", message.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    normalized = re.sub(r"[-‐‑‒–—]+", " ", without_marks)
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _words(message: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[\wÀ-ÿ]+", message)}


def _yes_no(message: str) -> bool | None:
    """Return a boolean only for an exact canonical control response."""

    normalized = _normalize(message)
    if normalized in _YES_VALUES:
        return True
    if normalized in _NO_VALUES:
        return False
    return None


def _explicit_language(message: str) -> Language | None:
    normalized = _normalize(message)
    if normalized in {"english", "ingles"}:
        return Language.EN
    if normalized in {"spanish", "espanol"}:
        return Language.ES
    language_only = re.fullmatch(r"(?:in|en) (english|ingles|spanish|espanol)", normalized)
    if language_only is not None:
        return Language.EN if language_only.group(1) in {"english", "ingles"} else Language.ES
    command = re.fullmatch(
        r"(?:(?:please|por favor) )?(?:continue|continua|speak|habla|respond|responde|reply|use|usa)"
        r"(?: in| en| using| usando)? (english|ingles|spanish|espanol)",
        normalized,
    )
    if command is not None:
        return Language.EN if command.group(1) in {"english", "ingles"} else Language.ES
    return None


def _is_disclosure_acknowledgement(message: str) -> bool:
    """Recognize a short acknowledgement without maintaining phrase variants."""

    normalized = _normalize(message)
    if normalized in _DISCLOSURE_ACKNOWLEDGEMENTS:
        return True
    # Politeness markers are syntax, not additional semantic answers.  Keep
    # the base vocabulary small so phrases such as ``Yep`` or ``I agree`` stay
    # with the semantic interpreter.
    match = re.fullmatch(r"(?:please )?(.+?)(?: please| por favor)?", normalized)
    return bool(match and match.group(1) in _DISCLOSURE_ACKNOWLEDGEMENTS)


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
    """Accept only a short, title-cased, alphabetic name-shaped answer.

    Bare names are safe to recover while the full-name field is active because
    the parser requires a proper-name shape. Lowercase prose, one-word
    replies, labels, and compound turns remain on the semantic model path.
    """

    candidate = " ".join(value.split()).strip(" .,!?;:¡¿")
    words = candidate.split()
    if not 2 <= len(words) <= 6:
        return None
    if any(not word or not word[0].isupper() for word in words):
        return None
    return _simple_name(candidate)


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


def _city_zone_mentions(
    text: str,
    city: str,
    service_area_matcher: ServiceAreaMatcher,
) -> list[ServiceArea]:
    """Find configured zones mentioned in text under one trusted city.

    A candidate can answer a city-level offer with a conversational phrase such
    as ``Centro is fine``.  The full phrase is not itself a catalogue alias,
    but the concrete zone is.  Restricting the scan to the already-known city
    prevents a shared zone name (``Centro`` occurs in several cities) from
    becoming an ungrounded global match.
    """

    city_key = normalize_location(city)
    normalized = normalize_location(text)
    if not city_key or not normalized:
        return []
    matches: dict[str, ServiceArea] = {}
    for area in service_area_matcher.catalog.areas:
        city_values = (area.city, *area.city_aliases)
        if not any(normalize_location(value) == city_key for value in city_values):
            continue
        if any(
            _phrase_in_text(normalized, normalize_location(candidate))
            for candidate in (area.zone, *area.aliases)
        ):
            matches[area.id] = area
    return list(matches.values())


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


def _location_patch_is_grounded(
    state: ScreeningState,
    message: str,
    patch: ExtractedLocation,
    service_area_matcher: ServiceAreaMatcher,
) -> bool:
    """Return whether a provided model location belongs to this turn.

    ``provided=true`` is an extraction claim, not provenance.  A model can
    echo a previously discussed location from bounded history while answering
    a different field.  Before that claim reaches reconciliation, require
    support from the current message or from a catalogue-grounded zone phrase
    in the message under the trusted city context.
    """

    raw = _extract_location_raw(patch)
    if not raw:
        return False
    if _location_text_is_grounded(raw, message, patch, service_area_matcher):
        return True

    pending = state.pending_confirmation
    contextual_city = patch.city
    if contextual_city is None and pending is not None and pending.reason == "service_area_city":
        contextual_city = state.location.city

    if contextual_city:
        mentioned = _city_zone_mentions(message, contextual_city, service_area_matcher)
        if len(mentioned) == 1:
            area = mentioned[0]
            raw_tokens = set(normalize_location(raw).split())
            zone_tokens = set(normalize_location(area.zone).split())
            patch_zone = normalize_location(patch.zone or "")
            zone_is_in_message = (
                _phrase_in_text(normalize_location(message), patch_zone) if patch_zone else False
            )
            # The model may canonicalize ``Centro is fine`` to ``Madrid
            # Centro``. Accept that only when the concrete zone is present in
            # the candidate message and in the proposed value.
            candidate_match = service_area_matcher.match(
                raw,
                city=area.city,
                zone=patch.zone or area.zone,
            )
            if (
                (zone_tokens <= raw_tokens or zone_is_in_message)
                and candidate_match.area is not None
                and candidate_match.area.id == area.id
            ):
                return True

    # A complete alias in the current message can ground a provider's
    # normalized/canonical spelling even when its evidence is not verbatim.
    current_areas = _catalog_area_mentions(message, service_area_matcher)
    model_match = service_area_matcher.match(
        raw,
        city=contextual_city,
        zone=patch.zone,
    )
    return (
        len(current_areas) == 1
        and model_match.area is not None
        and next(iter(current_areas.values()))[0].id == model_match.area.id
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


def _closed_location_interpretation(
    state: ScreeningState,
    message: str,
    *,
    service_area_matcher: ServiceAreaMatcher | None,
) -> InterpreterResult | None:
    """Interpret a closed location answer after neutral model output.

    This is intentionally narrower than :func:`recover_location_answer`,
    which also inspects a provider patch and exact aliases embedded in prose.
    With no usable typed patch to ground, only a complete catalogue alias, a
    bare known city, or a zone answer grounded by an existing city offer is
    eligible. In particular, a fuzzy suggestion, unsupported city, ambiguous
    phrase, or natural-language sentence remains model-owned.
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
            selected = (direct_match, "fallback_exact_alias")
        elif pending is None:
            # A bare known city is represented as a city-level offer by
            # reconciliation.  It never selects the first catalogue area.
            selected = (direct_match, "fallback_known_city")

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
                    selected = (contextual_match, "fallback_pending_city_zone")

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

    The provider is still responsible for open-ended extraction. This narrow
    fallback runs after every provider turn so every non-empty location patch
    is first grounded in the current candidate message; stale echoes from
    bounded history are discarded even when a provider forgets the
    ``provided`` flag. It accepts only an exact catalogue alias, a known city,
    or a zone grounded by a trusted city offer. It never turns a fuzzy
    suggestion into an eligible area and never trusts a structured city/zone
    hint that is contradicted by the candidate message.
    """

    interpretation = interpreted.interpretation
    if interpretation.prompt_injection_detected or interpretation.sensitive_data_detected:
        return interpreted

    existing_patch = interpretation.location
    has_location_claim = existing_patch is not None and (
        existing_patch.provided or bool(_extract_location_raw(existing_patch))
    )
    if (
        has_location_claim
        and existing_patch is not None
        and not _location_patch_is_grounded(
            state,
            message,
            existing_patch,
            service_area_matcher,
        )
    ):
        # ``provided`` is an extraction flag, not provenance. Providers can
        # echo a previous location from conversation history (with either
        # ``provided=true`` or a non-empty default patch) while the candidate
        # answers a different field. Discard only that stale patch; other
        # typed updates from the same turn remain available to reconciliation.
        usage = dict(interpreted.usage)
        usage.update(
            {
                "location_patch_discarded": True,
                "location_patch_discard_reason": "not_grounded_in_current_message",
            }
        )
        interpretation = interpretation.model_copy(update={"location": None})
        interpreted = replace(interpreted, interpretation=interpretation, usage=usage)
        existing_patch = None

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
    location_active = (
        state.current_field is ScreeningField.LOCATION
        or (pending is not None and pending.field is ScreeningField.LOCATION)
        or (existing_patch is not None and bool(_extract_location_raw(existing_patch)))
    )
    if not location_active:
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
    # ``provided`` bit. Reuse it only when its evidence is grounded in the
    # current user message. Pending city offers additionally provide trusted
    # city context for a short answer such as ``Centro``. The same grounding
    # check is applied to ``provided=true`` patches above so stale history
    # cannot create a correction proposal.
    if selected is None and existing_patch is not None:
        raw = _extract_location_raw(existing_patch)
        if raw and _location_patch_is_grounded(
            state,
            message_text,
            existing_patch,
            service_area_matcher,
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

            # The model may return the whole natural phrase (for example,
            # ``Centro is fine``) instead of a catalogue alias. If a unique
            # zone is grounded in the trusted city context, canonicalize it
            # rather than leaving a valid zone as unsupported text.
            if selected is None and patch_city:
                mentioned = _city_zone_mentions(
                    message_text,
                    patch_city,
                    service_area_matcher,
                )
                if len(mentioned) == 1:
                    area = mentioned[0]
                    area_match = service_area_matcher.match(
                        area.zone,
                        city=area.city,
                        zone=area.zone,
                    )
                    if area_match.status is LocationMatchStatus.EXACT and area_match.area:
                        selected = (
                            area.zone,
                            area_match,
                            area.city,
                            area.zone,
                            "model_location_zone_context",
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
            else:
                mentioned = _city_zone_mentions(message_text, city, service_area_matcher)
                if len(mentioned) == 1:
                    area = mentioned[0]
                    area_match = service_area_matcher.match(
                        area.zone,
                        city=area.city,
                        zone=area.zone,
                    )
                    if area_match.status is LocationMatchStatus.EXACT and area_match.area:
                        selected = (
                            area.zone,
                            area_match,
                            area.city,
                            area.zone,
                            "pending_city_zone_context",
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


def interpret_deterministically(
    state: ScreeningState,
    message: str,
    *,
    service_area_matcher: ServiceAreaMatcher | None = None,
) -> InterpreterResult | None:
    """Return a safe typed interpretation, or None for model-owned prose."""

    normalized = _normalize(message)
    base = _base(message, state)

    # Exact conversation controls are safe regardless of the pending field.
    # Natural opt-out prose (for example, "I don't want to continue") remains
    # model-owned rather than growing this closed vocabulary indefinitely.
    if normalized in _EXACT_OPT_OUT:
        base["intent"] = TurnIntent.OPT_OUT
        return InterpreterResult(
            interpretation=TurnInterpretation(
                **base,
                opt_out_requested=True,
            ),
            usage={"requests": 0, "deterministic": True, "deterministic_reason": "opt_out"},
        )

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

    if state.faq_offer_made and state.candidate_confirmed and not state.faq_completed:
        has_questions = _yes_no(message)
        if has_questions is not None and normalized in _YES_VALUES | _NO_VALUES:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    faq_complete=not has_questions,
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "faq_completion_control",
                },
            )

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

    # These are exact canonical answers to closed enum questions. Longer,
    # compound, corrective, or ambiguous wording still goes to the semantic
    # interpreter so this module does not become a phrase dictionary.
    if state.current_field is ScreeningField.AVAILABILITY:
        availability = _EXACT_AVAILABILITY.get(normalized)
        if availability is not None:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    availability=ExtractedValue(
                        value=[availability], provided=True, evidence=message[:500]
                    ),
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "availability_control",
                },
            )

    if state.current_field is ScreeningField.PREFERRED_SCHEDULE:
        schedule = _EXACT_SCHEDULE.get(normalized)
        if schedule is not None:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    preferred_schedule=ExtractedValue(
                        value=schedule, provided=True, evidence=message[:500]
                    ),
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "schedule_control",
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

    if not state.disclosure_acknowledged and _is_disclosure_acknowledgement(message):
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

    location = _closed_location_interpretation(
        state,
        message,
        service_area_matcher=service_area_matcher,
    )
    if location is not None:
        return location

    # Start availability is intentionally stored as candidate-authored text.
    # It is not an eligibility rule and does not require the application to
    # infer a calendar date. If the model produced no usable interpretation,
    # retaining a direct answer verbatim is safer than forcing the candidate
    # through a provider-error loop or growing a phrase-specific parser.
    if state.current_field is ScreeningField.START_AVAILABILITY:
        raw_start = " ".join(message.split()).strip()
        if raw_start and not _looks_like_question(raw_start):
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    start_availability=StartAvailabilityExtraction(
                        raw_value=raw_start[:300],
                        precision="unknown",
                        provided=True,
                        evidence=message[:500],
                    ),
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": "start_availability_verbatim",
                },
            )

    # Natural-language field answers remain model-owned. An explicit name
    # prefix and a short title-cased bare name are the only name shortcuts;
    # compound turns and lowercase prose remain on the semantic model path.
    if state.current_field is ScreeningField.FULL_NAME:
        stripped = message.strip()
        name = _simple_name(stripped) if _NAME_PREFIX.match(stripped) else _bare_name(stripped)
        if name is not None:
            return InterpreterResult(
                interpretation=TurnInterpretation(
                    **base,
                    disclosure_acknowledged=True,
                    full_name=ExtractedValue(value=name, provided=True, evidence=message[:500]),
                ),
                usage={
                    "requests": 0,
                    "deterministic": True,
                    "deterministic_reason": (
                        "name_prefix" if _NAME_PREFIX.match(stripped) else "name_answer"
                    ),
                },
            )

    return None


def is_exact_opt_out(message: str) -> bool:
    """Return whether ``message`` is an application-owned opt-out control.

    The coordinator keeps this one safety-critical control provider-free.  It
    is deliberately exposed separately from :func:`interpret_deterministically`
    so that checking for an exact opt-out does not accidentally put every
    deterministic shortcut ahead of the language model.
    """

    return _normalize(message) in _EXACT_OPT_OUT


__all__ = ["interpret_deterministically", "is_exact_opt_out"]
