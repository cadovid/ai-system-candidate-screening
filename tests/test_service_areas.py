from __future__ import annotations

import json
from pathlib import Path

import pytest

from candidate_screening.domain.enums import LocationMatchStatus
from candidate_screening.domain.service_areas import (
    ServiceArea,
    ServiceAreaCatalog,
    ServiceAreaMatcher,
    normalize_location,
)


def test_normalize_location_handles_accents_punctuation_ampersands_and_blanks() -> None:
    assert normalize_location("  Málaga / Centro  ") == "malaga centro"
    assert normalize_location("R&D, Zone") == "r y d zone"
    assert normalize_location("---") == ""


def test_catalog_loads_wrapped_data_and_rejects_invalid_or_duplicate_entries(
    tmp_path: Path,
) -> None:
    wrapped = tmp_path / "areas.json"
    area = {
        "id": "xx-one",
        "country": "xx",
        "city": "Example",
        "zone": "Central",
        "aliases": [" Example ", "example"],
    }
    wrapped.write_text(json.dumps({"areas": [area]}), encoding="utf-8")
    catalog = ServiceAreaCatalog.from_file(wrapped)
    assert catalog.by_id("xx-one") is not None
    assert catalog.areas[0].country == "XX"
    assert catalog.areas[0].aliases == ["Example"]
    assert catalog.areas[0].display_name == "Example — Central"

    wrapped.write_text(json.dumps({"areas": [area, area]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        ServiceAreaCatalog.from_file(wrapped)
    wrapped.write_text(json.dumps({"areas": "bad"}), encoding="utf-8")
    with pytest.raises(ValueError, match="service-area data"):
        ServiceAreaCatalog.from_file(wrapped)


def test_service_area_model_validates_country_and_required_text() -> None:
    with pytest.raises(ValueError, match="country"):
        ServiceArea(id="x", country="USA", city="City", zone="Zone")
    with pytest.raises(ValueError, match="country"):
        ServiceArea(id="x", country="1A", city="City", zone="Zone")
    with pytest.raises(ValueError, match="service-area text"):
        ServiceArea(id="x", country="US", city=" ", zone="Zone")


def test_match_covers_unresolved_exact_ambiguous_fuzzy_and_unsupported() -> None:
    matcher = ServiceAreaMatcher.from_file(Path("data/service_areas/service_areas.json"))

    unresolved = matcher.match("   ")
    assert unresolved.status is LocationMatchStatus.UNRESOLVED
    assert unresolved.confidence == 0

    exact = matcher.match("MÁLAGA centro")
    assert exact.status is LocationMatchStatus.EXACT
    assert exact.area is not None and exact.area.id == "es-mal-centro"
    assert exact.suggestion_ids == []

    ambiguous = matcher.match("Madrid")
    assert ambiguous.status is LocationMatchStatus.AMBIGUOUS
    assert ambiguous.area is None
    assert len(ambiguous.suggestions) == 2

    fuzzy = ServiceAreaMatcher.from_file(Path("data/service_areas/service_areas.json")).match(
        "Madrd centro"
    )
    assert fuzzy.status is LocationMatchStatus.NEEDS_CONFIRMATION
    assert fuzzy.suggestion_ids == ["es-mad-centro"]
    assert fuzzy.confidence >= matcher.suggestion_threshold

    unsupported = matcher.match("Paris")
    assert unsupported.status is LocationMatchStatus.UNSUPPORTED
    assert unsupported.area is None
    assert unsupported.suggestions == []


def test_english_location_aliases_and_known_city_offers_are_conservative() -> None:
    matcher = ServiceAreaMatcher.from_file(Path("data/service_areas/service_areas.json"))

    city_center = matcher.match("The city center of Madrid")
    assert city_center.status is LocationMatchStatus.EXACT
    assert city_center.area is not None and city_center.area.id == "es-mad-centro"
    assert city_center.city == "Madrid"

    # A city is not silently treated as its first/only zone.  The caller can
    # present the configured options and obtain explicit confirmation.
    madrid = matcher.match("Madrid")
    assert madrid.is_city_level is True
    assert madrid.city == "Madrid"
    assert madrid.suggestion_ids == ["es-mad-centro", "es-mad-salamanca"]

    valencia = matcher.match("Valencia")
    assert valencia.is_city_level is True
    assert valencia.suggestion_ids == ["es-val-ciutat"]

    # Structured city/zone output from a model is handled without requiring
    # the provider to preserve the complete natural-language phrase.
    structured = matcher.match("city center", city="Madrid", zone="city center")
    assert structured.status is LocationMatchStatus.EXACT
    assert structured.area is not None and structured.area.id == "es-mad-centro"

    # Some model responses contain the short zone phrase but omit ``zone``;
    # the known city context still permits the same deterministic exact match.
    short_structured = matcher.match("city center", city="Madrid")
    assert short_structured.status is LocationMatchStatus.EXACT
    assert short_structured.area is not None and short_structured.area.id == "es-mad-centro"

    mexico_city = matcher.match("Mexico City")
    assert mexico_city.is_city_level is True
    assert mexico_city.city == "Ciudad de México"
    assert mexico_city.suggestion_ids == ["mx-cdmx-roma", "mx-cdmx-polanco"]


def test_known_city_with_unknown_zone_returns_city_offer_not_rejection() -> None:
    matcher = ServiceAreaMatcher.from_file(Path("data/service_areas/service_areas.json"))
    result = matcher.match("Madrid north", city="Madrid", zone="north")
    assert result.is_city_level is True
    assert result.status is LocationMatchStatus.AMBIGUOUS
    assert result.city == "Madrid"
    assert result.suggestion_ids == ["es-mad-centro", "es-mad-salamanca"]


def test_raw_unknown_city_cannot_be_overridden_by_a_hallucinated_city_hint() -> None:
    matcher = ServiceAreaMatcher.from_file(Path("data/service_areas/service_areas.json"))
    result = matcher.match("Bilbao", city="Madrid")
    assert result.status is LocationMatchStatus.UNSUPPORTED
    assert result.city is None
    assert result.suggestions == []

    contradictory_zone = matcher.match("Bilbao", city="Madrid", zone="center")
    assert contradictory_zone.status is LocationMatchStatus.UNSUPPORTED
    assert contradictory_zone.city is None
    assert contradictory_zone.area is None


def test_match_threshold_can_make_fuzzy_input_unsupported_and_confirm_is_conservative() -> None:
    matcher = ServiceAreaMatcher(
        ServiceAreaCatalog.from_file(Path("data/service_areas/service_areas.json")),
        suggestion_threshold=0.99,
    )
    unsupported = matcher.match("Madrd centro")
    assert unsupported.status is LocationMatchStatus.UNSUPPORTED

    exact = matcher.match("Madrid centro")
    confirmed = matcher.confirm(exact, "es-mad-centro")
    assert confirmed.status is LocationMatchStatus.EXACT
    assert confirmed.confidence == 1

    fuzzy = ServiceAreaMatcher.from_file(Path("data/service_areas/service_areas.json")).match(
        "Madrd centro"
    )
    confirmed_fuzzy = matcher.confirm(fuzzy, "es-mad-centro")
    assert confirmed_fuzzy.area is not None
    assert confirmed_fuzzy.area.id == "es-mad-centro"
    with pytest.raises(ValueError, match="suggestions"):
        matcher.confirm(unsupported, "es-mad-centro")


def test_matcher_rejects_invalid_thresholds() -> None:
    catalog = ServiceAreaCatalog.from_file(Path("data/service_areas/service_areas.json"))
    with pytest.raises(ValueError, match="threshold"):
        ServiceAreaMatcher(catalog, suggestion_threshold=0)
    with pytest.raises(ValueError, match="threshold"):
        ServiceAreaMatcher(catalog, suggestion_threshold=1)


def test_matcher_skips_empty_aliases_and_caps_suggestions_at_five() -> None:
    areas = [
        ServiceArea(
            id=f"xx-{index}",
            country="XX",
            city=f"City {index}",
            zone="Zone",
            aliases=["!!!"],
        )
        for index in range(6)
    ]
    matcher = ServiceAreaMatcher(ServiceAreaCatalog(areas=areas), suggestion_threshold=0.01)

    result = matcher.match("z")

    assert result.status is LocationMatchStatus.AMBIGUOUS
    assert len(result.suggestions) == 5
