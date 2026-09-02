from __future__ import annotations

from pathlib import Path

from candidate_screening.application.faq import FAQCatalog
from candidate_screening.domain.enums import Language


def test_faq_matching_is_accent_insensitive_and_localized() -> None:
    catalog = FAQCatalog.from_file(Path("data/faq/faq.json"))

    match = catalog.retrieve("¿Qué horarios hay?", Language.ES)
    assert match is not None
    assert match.entry.id == "schedules"
    assert "opciones" in (catalog.answer("¿Qué horarios hay?", Language.ES) or "")

    # A code-switched question still receives the requested language answer.
    assert "Morning" in (catalog.answer("What horarios are available?", Language.EN) or "")


def test_faq_unknown_question_returns_none() -> None:
    catalog = FAQCatalog.from_file(Path("data/faq/faq.json"))
    assert catalog.answer("What is the meaning of life?", Language.EN) is None
