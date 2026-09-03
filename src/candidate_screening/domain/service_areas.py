"""Configurable service-area catalogue and conservative matching."""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import LocationMatchStatus

_LOCATION_FILLER_TOKENS = {
    "a",
    "an",
    "the",
    "of",
    "in",
    "at",
    "city",
    "area",
    "zone",
    "s",
    "de",
    "del",
    "la",
    "el",
}
_GENERIC_LOCATION_TOKENS = _LOCATION_FILLER_TOKENS | {
    "centre",
    "center",
    "downtown",
}


def normalize_location(value: str) -> str:
    """Normalize accents, punctuation, case, and whitespace for exact lookup."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = without_marks.casefold().replace("&", " y ")
    folded = re.sub(r"[\u2010-\u2015_/|,;:()\[\]{}]+", " ", folded)
    folded = re.sub(r"[^\w\s-]", " ", folded, flags=re.UNICODE)
    folded = re.sub(r"\s+", " ", folded).strip(" -")
    return folded


class ServiceArea(BaseModel):
    """One fictional service area; this catalogue is intentionally data-driven."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=2, max_length=2)
    city: str = Field(min_length=1, max_length=120)
    zone: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    city_aliases: list[str] = Field(default_factory=list, max_length=10)
    timezone: str = Field(default="Europe/Madrid", max_length=80)

    @field_validator("id", "city", "zone", "timezone")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = " ".join(value.split()).strip()
        if not value:
            raise ValueError("service-area text fields cannot be blank")
        return value

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("country must be a two-letter code")
        return value

    @field_validator("aliases", "city_aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = " ".join(value.split()).strip()
            if normalized and normalized.casefold() not in {item.casefold() for item in result}:
                result.append(normalized)
        return result

    @property
    def display_name(self) -> str:
        return f"{self.city} — {self.zone}"


class ServiceAreaCatalog(BaseModel):
    """Validated catalogue with a loader kept outside prompts and business rules."""

    model_config = ConfigDict(extra="forbid")

    areas: list[ServiceArea] = Field(min_length=1)

    @classmethod
    def from_file(cls, path: str | Path) -> ServiceAreaCatalog:
        source = Path(path)
        with source.open(encoding="utf-8") as file:
            payload = json.load(file)
        # Support both the repository's simple list format and a wrapped
        # ``{"areas": [...]}`` format used by deployment/config tooling.
        if isinstance(payload, dict):
            payload = cast(dict[str, Any], payload).get("areas")
        if not isinstance(payload, list):
            raise ValueError("service-area data must be a list or an object with an areas list")
        return cls.model_validate({"areas": payload})

    @model_validator(mode="after")
    def validate_unique_ids(self) -> ServiceAreaCatalog:
        ids = [area.id for area in self.areas]
        if len(ids) != len(set(ids)):
            raise ValueError("service-area ids must be unique")
        return self

    def by_id(self, area_id: str) -> ServiceArea | None:
        return next((area for area in self.areas if area.id == area_id), None)


class ServiceAreaMatch(BaseModel):
    """Match result; fuzzy suggestions are never accepted automatically."""

    model_config = ConfigDict(extra="forbid")

    raw_value: str
    normalized_value: str
    status: LocationMatchStatus
    area: ServiceArea | None = None
    # A canonical city is populated for exact area matches and for the
    # conservative city-level offer path.  It is deliberately separate from
    # ``area``: knowing a city must never be treated as knowing a zone.
    city: str | None = Field(default=None, max_length=120)
    suggestions: list[ServiceArea] = Field(
        default_factory=lambda: list[ServiceArea](), max_length=5
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @property
    def suggestion_ids(self) -> list[str]:
        return [area.id for area in self.suggestions]

    @property
    def is_city_level(self) -> bool:
        """Whether this is a known-city result without a selected zone."""

        return self.area is None and self.city is not None and bool(self.suggestions)


class ServiceAreaMatcher:
    """Resolve exact aliases and provide conservative fuzzy suggestions."""

    def __init__(self, catalog: ServiceAreaCatalog, *, suggestion_threshold: float = 0.74) -> None:
        if not 0.0 < suggestion_threshold < 1.0:
            raise ValueError("suggestion threshold must be between zero and one")
        self.catalog = catalog
        self.suggestion_threshold = suggestion_threshold
        self._exact: dict[str, list[ServiceArea]] = {}
        self._cities: dict[str, list[ServiceArea]] = {}
        self._area_city_keys: dict[str, set[str]] = {}
        for area in catalog.areas:
            for city_value in [area.city, *area.city_aliases]:
                city_normalized = normalize_location(city_value)
                if city_normalized:
                    city_areas = self._cities.setdefault(city_normalized, [])
                    if area.id not in {item.id for item in city_areas}:
                        city_areas.append(area)
                    self._area_city_keys.setdefault(area.id, set()).add(city_normalized)
            # Bare city names are indexed separately.  Including them in the
            # exact area index would incorrectly accept ``Valencia`` as its
            # sole zone instead of asking whether the candidate can deliver in
            # the configured areas for that city.
            values = [area.display_name, f"{area.city} {area.zone}", *area.aliases]
            for value in values:
                normalized = normalize_location(value)
                if normalized:
                    self._exact.setdefault(normalized, []).append(area)

    @classmethod
    def from_file(
        cls, path: str | Path, *, suggestion_threshold: float = 0.74
    ) -> ServiceAreaMatcher:
        """Load a catalogue with an explicit fuzzy-suggestion threshold."""

        return cls(ServiceAreaCatalog.from_file(path), suggestion_threshold=suggestion_threshold)

    def _city_key(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = normalize_location(value)
        return normalized if normalized in self._cities else None

    def _infer_city_key(self, normalized_value: str) -> str | None:
        """Find a catalogue city mentioned in a natural-language location."""

        padded = f" {normalized_value} "
        # Prefer longer names first (for example, ``ciudad de mexico`` over a
        # shorter token that may happen to occur in the same phrase).
        for city_key in sorted(self._cities, key=len, reverse=True):
            if f" {city_key} " in padded:
                return city_key
        return None

    def _city_result(
        self,
        *,
        raw_value: str,
        normalized_value: str,
        city_key: str,
        confidence: float,
    ) -> ServiceAreaMatch:
        areas = self._cities[city_key]
        return ServiceAreaMatch(
            raw_value=raw_value,
            normalized_value=normalized_value,
            status=LocationMatchStatus.AMBIGUOUS,
            city=areas[0].city,
            suggestions=areas[:5],
            confidence=round(confidence, 3),
        )

    def _zone_matches(self, city_key: str, zone: str) -> list[ServiceArea]:
        normalized_zone = normalize_location(zone)
        if not normalized_zone:
            return []
        matches: list[ServiceArea] = []
        for area in self._cities[city_key]:
            candidates = [area.zone, *area.aliases]
            if any(
                normalized_zone == candidate_normalized
                or candidate_normalized.endswith(f" {normalized_zone}")
                or candidate_normalized.startswith(f"{normalized_zone} ")
                for candidate in candidates
                for candidate_normalized in [normalize_location(candidate)]
            ):
                matches.append(area)
        return matches

    def _zone_matches_in_text(self, city_key: str, value: str) -> list[ServiceArea]:
        """Find a zone phrase while retaining a known city context.

        Providers sometimes return ``city="Madrid"`` and a short
        ``raw_value="city center"`` instead of preserving the complete
        candidate phrase.  This helper accepts a zone-only phrase only when
        its tokens are contained in one of that city's configured zone names
        or aliases.  It therefore does not turn an unrelated raw city into a
        valid area merely because a model supplied a conflicting city hint.
        """

        direct = self._zone_matches(city_key, value)
        if direct:
            return direct
        normalized = normalize_location(value)
        if not normalized:
            return []
        raw_tokens = set(normalized.split())
        matches: list[ServiceArea] = []
        for area in self._cities[city_key]:
            city_tokens = {
                token
                for city_value in self._area_city_keys.get(area.id, set())
                for token in city_value.split()
            }
            # If the raw phrase explicitly names another city, this area is
            # not compatible with the structured city hint.
            if raw_tokens & city_tokens and not self._contains_city(normalized, city_key):
                continue
            raw_zone_tokens = raw_tokens - city_tokens
            if not raw_zone_tokens:
                continue
            for candidate in [area.zone, *area.aliases]:
                candidate_tokens = set(normalize_location(candidate).split()) - city_tokens
                raw_specific_tokens = raw_zone_tokens - _GENERIC_LOCATION_TOKENS
                candidate_specific_tokens = candidate_tokens - _GENERIC_LOCATION_TOKENS
                if raw_specific_tokens:
                    compatible = raw_specific_tokens <= candidate_specific_tokens
                else:
                    # Phrases such as ``the city center`` consist entirely
                    # of generic location words.  Require the concrete
                    # descriptor (``center``, ``downtown``, etc.) to occur in
                    # the candidate alias so they cannot match every area in
                    # the city.
                    concrete_tokens = raw_zone_tokens - _LOCATION_FILLER_TOKENS
                    compatible = bool(concrete_tokens & candidate_tokens)
                if compatible and raw_zone_tokens <= candidate_tokens | _GENERIC_LOCATION_TOKENS:
                    matches.append(area)
                    break
        return matches

    @staticmethod
    def _contains_city(normalized_value: str, city_key: str) -> bool:
        return f" {city_key} " in f" {normalized_value} "

    def _raw_zone_context_is_compatible(
        self,
        normalized_value: str,
        city_key: str,
        area: ServiceArea,
    ) -> bool:
        """Ensure structured city/zone hints do not override raw text."""

        if self._contains_city(normalized_value, city_key):
            return True
        # A raw phrase naming a different configured city is never a zone-only
        # expression for this city.  Unknown city names are rejected below by
        # the token-subset check instead of being mapped by model suggestion.
        if any(
            other_key != city_key and self._contains_city(normalized_value, other_key)
            for other_key in self._cities
        ):
            return False
        raw_tokens = set(normalized_value.split())
        city_tokens = {
            token
            for city_value in self._area_city_keys.get(area.id, set())
            for token in city_value.split()
        }
        raw_zone_tokens = raw_tokens - city_tokens
        if not raw_zone_tokens:
            return False
        for candidate in [area.zone, *area.aliases]:
            candidate_tokens = set(normalize_location(candidate).split()) - city_tokens
            raw_specific_tokens = raw_zone_tokens - _GENERIC_LOCATION_TOKENS
            candidate_specific_tokens = candidate_tokens - _GENERIC_LOCATION_TOKENS
            if raw_specific_tokens:
                compatible = raw_specific_tokens <= candidate_specific_tokens
            else:
                concrete_tokens = raw_zone_tokens - _LOCATION_FILLER_TOKENS
                compatible = bool(concrete_tokens & candidate_tokens)
            if compatible and raw_zone_tokens <= candidate_tokens | _GENERIC_LOCATION_TOKENS:
                return True
        return False

    def match(
        self,
        value: str,
        *,
        city: str | None = None,
        zone: str | None = None,
    ) -> ServiceAreaMatch:
        """Match a raw location, optionally using structured city/zone hints.

        Exact catalogue aliases are authoritative.  A recognized city without
        a safely identified zone returns an ambiguous city-level result so the
        conversation can present all configured zones and obtain an explicit
        candidate confirmation.  Fuzzy results remain suggestions only.
        """

        raw = value.strip()
        normalized = normalize_location(raw)
        if not normalized:
            return ServiceAreaMatch(
                raw_value=value,
                normalized_value="",
                status=LocationMatchStatus.UNRESOLVED,
                confidence=0.0,
            )

        explicit_city_key = self._city_key(city)
        inferred_city_key = self._infer_city_key(normalized)
        if inferred_city_key is not None:
            # The raw candidate phrase is the safer source when structured
            # extraction and raw text disagree.  A hallucinated city hint
            # must not turn an unknown city into a valid configured one.
            city_key = inferred_city_key
        else:
            city_key = None

            # A short raw phrase may be only the zone while the model supplies
            # the city separately.  Use that hint only when the raw phrase is
            # itself compatible with a configured zone in that city.  In
            # particular, ``value="Bilbao", city="Madrid", zone="center"``
            # must not be accepted as Madrid Centro.
            if explicit_city_key is not None:
                context_value = zone or raw
                context_matches = self._zone_matches_in_text(
                    explicit_city_key,
                    context_value,
                )
                compatible_matches = [
                    area
                    for area in context_matches
                    if self._raw_zone_context_is_compatible(
                        normalized,
                        explicit_city_key,
                        area,
                    )
                ]
                if len(compatible_matches) == 1:
                    area = compatible_matches[0]
                    return ServiceAreaMatch(
                        raw_value=value,
                        normalized_value=normalized,
                        status=LocationMatchStatus.EXACT,
                        area=area,
                        city=area.city,
                        confidence=1.0,
                    )
                if len(compatible_matches) > 1:
                    return ServiceAreaMatch(
                        raw_value=value,
                        normalized_value=normalized,
                        status=LocationMatchStatus.AMBIGUOUS,
                        city=self._cities[explicit_city_key][0].city,
                        suggestions=compatible_matches[:5],
                        confidence=0.8,
                    )
                if self._contains_city(normalized, explicit_city_key):
                    city_key = explicit_city_key

        # A bare city is intentionally not an area match, even when that city
        # currently has only one configured zone.
        if city_key is not None and normalized == city_key and not zone:
            return self._city_result(
                raw_value=value,
                normalized_value=normalized,
                city_key=city_key,
                confidence=1.0,
            )

        exact_by_id: dict[str, ServiceArea] = {
            area.id: area for area in self._exact.get(normalized, [])
        }
        exact = list(exact_by_id.values())
        if len(exact) == 1:
            return ServiceAreaMatch(
                raw_value=value,
                normalized_value=normalized,
                status=LocationMatchStatus.EXACT,
                area=exact[0],
                city=exact[0].city,
                confidence=1.0,
            )
        if len(exact) > 1:
            exact_cities = {area.city for area in exact}
            return ServiceAreaMatch(
                raw_value=value,
                normalized_value=normalized,
                status=LocationMatchStatus.AMBIGUOUS,
                city=next(iter(exact_cities)) if len(exact_cities) == 1 else None,
                suggestions=exact[:5],
                confidence=0.5,
            )

        # Structured extraction may identify a city and zone even when the
        # raw phrase does not exactly equal a catalogue alias.  Only exact
        # zone/alias matches are accepted here; a missing/unknown zone falls
        # back to the explicit city-level offer below.
        if city_key is not None and not zone and normalized != city_key:
            text_zone_matches = self._zone_matches_in_text(city_key, raw)
            if len(text_zone_matches) == 1:
                area = text_zone_matches[0]
                return ServiceAreaMatch(
                    raw_value=value,
                    normalized_value=normalized,
                    status=LocationMatchStatus.EXACT,
                    area=area,
                    city=area.city,
                    confidence=1.0,
                )
            if len(text_zone_matches) > 1:
                return ServiceAreaMatch(
                    raw_value=value,
                    normalized_value=normalized,
                    status=LocationMatchStatus.AMBIGUOUS,
                    city=self._cities[city_key][0].city,
                    suggestions=text_zone_matches[:5],
                    confidence=0.8,
                )
        if city_key is not None and zone:
            zone_matches = self._zone_matches(city_key, zone)
            if len(zone_matches) == 1:
                area = zone_matches[0]
                return ServiceAreaMatch(
                    raw_value=value,
                    normalized_value=normalized,
                    status=LocationMatchStatus.EXACT,
                    area=area,
                    city=area.city,
                    confidence=1.0,
                )
            if len(zone_matches) > 1:
                return ServiceAreaMatch(
                    raw_value=value,
                    normalized_value=normalized,
                    status=LocationMatchStatus.AMBIGUOUS,
                    city=self._cities[city_key][0].city,
                    suggestions=zone_matches[:5],
                    confidence=0.8,
                )
            # Some model providers return ``city`` and ``zone`` separately
            # while preserving only a short raw phrase.  Check composed
            # catalogue aliases (for example ``Madrid city center``) before
            # falling back to the city-level offer.
            city_name = self._cities[city_key][0].city
            composed_queries = {
                normalize_location(f"{city_name} {zone}"),
                normalize_location(f"{zone} {city_name}"),
                normalize_location(f"the {zone} of {city_name}"),
            }
            composed: dict[str, ServiceArea] = {}
            for query in composed_queries:
                for area in self._exact.get(query, []):
                    if normalize_location(area.city) == city_key:
                        composed[area.id] = area
            if len(composed) == 1:
                area = next(iter(composed.values()))
                return ServiceAreaMatch(
                    raw_value=value,
                    normalized_value=normalized,
                    status=LocationMatchStatus.EXACT,
                    area=area,
                    city=area.city,
                    confidence=1.0,
                )
            if len(composed) > 1:
                return ServiceAreaMatch(
                    raw_value=value,
                    normalized_value=normalized,
                    status=LocationMatchStatus.AMBIGUOUS,
                    city=city_name,
                    suggestions=list(composed.values())[:5],
                    confidence=0.8,
                )

        # The city is known but the phrase did not identify one of its zones.
        # Keep all options visible to the candidate rather than guessing from
        # a broad similarity score.
        if city_key is not None:
            return self._city_result(
                raw_value=value,
                normalized_value=normalized,
                city_key=city_key,
                confidence=0.9,
            )

        scored: list[tuple[float, ServiceArea]] = []
        for area in self.catalog.areas:
            for candidate in [
                area.display_name,
                f"{area.city} {area.zone}",
                *area.aliases,
            ]:
                candidate_normalized = normalize_location(candidate)
                # Similarity against generic words such as ``city`` or
                # ``centre`` can otherwise turn an unknown city (for example
                # ``Bilbao city centre``) into an unrelated Málaga/Sevilla
                # suggestion.  Require at least one shared token before a
                # fuzzy suggestion can be shown; exact aliases remain the
                # authoritative path above.
                overlap = (
                    set(normalized.split()) & set(candidate_normalized.split())
                ) - _GENERIC_LOCATION_TOKENS
                # Preserve the matcher's tiny-input behavior for deliberately
                # permissive/custom catalogues while preventing generic suffix
                # words from matching an unrelated unknown city.
                if not overlap and len(normalized) > 2:
                    continue
                score = SequenceMatcher(None, normalized, candidate_normalized).ratio()
                scored.append((score, area))
        ranked: list[ServiceArea] = []
        for score, area in sorted(scored, key=lambda item: item[0], reverse=True):
            if score >= self.suggestion_threshold and area.id not in {item.id for item in ranked}:
                ranked.append(area)
            if len(ranked) == 5:
                break

        best_score = max((score for score, _ in scored), default=0.0)
        if ranked:
            status = (
                LocationMatchStatus.NEEDS_CONFIRMATION
                if len(ranked) == 1
                else LocationMatchStatus.AMBIGUOUS
            )
            return ServiceAreaMatch(
                raw_value=value,
                normalized_value=normalized,
                status=status,
                city=None,
                suggestions=ranked,
                confidence=round(best_score, 3),
            )
        return ServiceAreaMatch(
            raw_value=value,
            normalized_value=normalized,
            status=LocationMatchStatus.UNSUPPORTED,
            confidence=round(best_score, 3),
        )

    def confirm(self, match: ServiceAreaMatch, area_id: str) -> ServiceAreaMatch:
        """Confirm a suggested area only when it was returned for this input."""

        if match.area is not None and match.area.id == area_id:
            return match.model_copy(
                update={
                    "status": LocationMatchStatus.EXACT,
                    "city": match.area.city,
                    "confidence": 1.0,
                }
            )
        allowed = {area.id: area for area in match.suggestions}
        area = allowed.get(area_id)
        if area is None:
            raise ValueError("area_id must be one of the match suggestions")
        return match.model_copy(
            update={
                "status": LocationMatchStatus.EXACT,
                "area": area,
                "city": area.city,
                "confidence": 1.0,
            }
        )
