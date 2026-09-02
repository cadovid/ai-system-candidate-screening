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

    @field_validator("aliases")
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
    suggestions: list[ServiceArea] = Field(
        default_factory=lambda: list[ServiceArea](), max_length=5
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @property
    def suggestion_ids(self) -> list[str]:
        return [area.id for area in self.suggestions]


class ServiceAreaMatcher:
    """Resolve exact aliases and provide conservative fuzzy suggestions."""

    def __init__(self, catalog: ServiceAreaCatalog, *, suggestion_threshold: float = 0.74) -> None:
        if not 0.0 < suggestion_threshold < 1.0:
            raise ValueError("suggestion threshold must be between zero and one")
        self.catalog = catalog
        self.suggestion_threshold = suggestion_threshold
        self._exact: dict[str, list[ServiceArea]] = {}
        for area in catalog.areas:
            values = [area.display_name, area.city, f"{area.city} {area.zone}", *area.aliases]
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

    def match(self, value: str) -> ServiceAreaMatch:
        raw = value.strip()
        normalized = normalize_location(raw)
        if not normalized:
            return ServiceAreaMatch(
                raw_value=value,
                normalized_value="",
                status=LocationMatchStatus.UNRESOLVED,
                confidence=0.0,
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
                confidence=1.0,
            )
        if len(exact) > 1:
            return ServiceAreaMatch(
                raw_value=value,
                normalized_value=normalized,
                status=LocationMatchStatus.AMBIGUOUS,
                suggestions=exact[:5],
                confidence=0.5,
            )

        scored: list[tuple[float, ServiceArea]] = []
        candidates: dict[str, ServiceArea] = {}
        for area in self.catalog.areas:
            candidates[area.id] = area
            for candidate in [
                area.display_name,
                area.city,
                f"{area.city} {area.zone}",
                *area.aliases,
            ]:
                candidate_normalized = normalize_location(candidate)
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
            return match.model_copy(update={"status": LocationMatchStatus.EXACT, "confidence": 1.0})
        allowed = {area.id: area for area in match.suggestions}
        area = allowed.get(area_id)
        if area is None:
            raise ValueError("area_id must be one of the match suggestions")
        return match.model_copy(
            update={"status": LocationMatchStatus.EXACT, "area": area, "confidence": 1.0}
        )
