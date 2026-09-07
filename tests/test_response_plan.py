from __future__ import annotations

import hashlib

import pytest

from candidate_screening.application.conversation_copy import render_response_plan
from candidate_screening.application.response_plan import (
    ResponseKind,
    ResponsePlan,
    choose_variant,
    random_variant_index,
    stable_variant_index,
)
from candidate_screening.domain.enums import Language, ScreeningField


def test_variant_selection_is_sha256_stable_and_injectable() -> None:
    key = "screening:en:full_name"
    expected = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") % 3

    def injected_selector(key: str, variant_count: int) -> int:
        _ = (key, variant_count)
        return 1

    assert stable_variant_index(key, 3) == expected
    assert stable_variant_index(key, 3) == stable_variant_index(key, 3)
    assert choose_variant(key, ("first", "second", "third"), selector=injected_selector) == "second"

    with pytest.raises(ValueError, match="positive"):
        stable_variant_index(key, 0)
    with pytest.raises(ValueError, match="empty"):
        choose_variant(key, ())
    assert 0 <= random_variant_index(key, 3) < 3
    with pytest.raises(ValueError, match="positive"):
        random_variant_index(key, 0)


@pytest.mark.parametrize(
    ("language", "field", "expected_terms"),
    [
        (Language.EN, ScreeningField.AVAILABILITY, ("full-time", "part-time", "weekend")),
        (
            Language.EN,
            ScreeningField.PREFERRED_SCHEDULE,
            ("morning", "afternoon", "evening", "flexible"),
        ),
        (
            Language.ES,
            ScreeningField.AVAILABILITY,
            ("tiempo completo", "tiempo parcial", "fines de semana"),
        ),
        (Language.ES, ScreeningField.PREFERRED_SCHEDULE, ("mañana", "tarde", "noche", "flexible")),
    ],
)
def test_canonical_availability_and_schedule_prompts_are_localized(
    language: Language,
    field: ScreeningField,
    expected_terms: tuple[str, ...],
) -> None:
    rendered = render_response_plan(ResponsePlan(ResponseKind.PROMPT, language, field=field))
    lowered = rendered.casefold()

    assert all(term.casefold() in lowered for term in expected_terms)


def test_faq_bridge_and_clarification_are_localized_and_field_specific() -> None:
    faq = render_response_plan(
        ResponsePlan(
            ResponseKind.FAQ_BRIDGE,
            Language.EN,
            field=ScreeningField.FULL_NAME,
            faq_answer="Morning shifts are available.",
        )
    )
    assert faq.startswith("Morning shifts are available.")
    assert "full name" in faq

    clarification = render_response_plan(
        ResponsePlan(ResponseKind.CLARIFICATION, Language.ES, field=ScreeningField.LOCATION)
    )
    assert "ciudad" in clarification.casefold()
    assert "zona de servicio" in clarification.casefold()


@pytest.mark.parametrize(
    "kind",
    [
        ResponseKind.QUALIFIED,
        ResponseKind.DISQUALIFIED,
        ResponseKind.NEEDS_REVIEW,
        ResponseKind.OPT_OUT,
        ResponseKind.MAX_TURNS,
        ResponseKind.SECURITY,
        ResponseKind.SENSITIVE,
        ResponseKind.TEMPORARY_FAILURE,
    ],
)
def test_terminal_safety_and_provider_copy_cannot_be_prefixed_by_faq_text(
    kind: ResponseKind,
) -> None:
    rendered = render_response_plan(
        ResponsePlan(
            kind,
            Language.EN,
            field=ScreeningField.FULL_NAME,
            faq_answer="UNTRUSTED FAQ TEXT",
        )
    )

    assert "UNTRUSTED FAQ TEXT" not in rendered
    assert not rendered.startswith("UNTRUSTED")
    if kind is ResponseKind.TEMPORARY_FAILURE:
        assert "temporarily unable" in rendered
    elif kind is ResponseKind.SECURITY:
        assert "internal instructions" in rendered
    elif kind is ResponseKind.SENSITIVE:
        assert "privacy" in rendered
