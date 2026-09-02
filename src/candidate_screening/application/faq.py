"""Deterministic lookup for the small fictional recruitment FAQ."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from candidate_screening.domain.enums import Language


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.casefold().split())


def _tokens(value: str) -> tuple[str, ...]:
    """Return accent-insensitive word tokens, retaining Spanish words."""

    return tuple(re.findall(r"[\w]+", _fold(value), flags=re.UNICODE))


def _word_hit(keyword: str, query_tokens: set[str]) -> bool:
    """Match exact words and the common singular/plural spelling variant."""

    if keyword in query_tokens:
        return True
    if len(keyword) > 4 and keyword.endswith("s") and keyword[:-1] in query_tokens:
        return True
    return len(keyword) > 4 and any(
        token.endswith("s") and token[:-1] == keyword for token in query_tokens
    )


@dataclass(frozen=True, slots=True)
class FAQMatch:
    """A deterministic FAQ hit and its evidence for an audit event."""

    entry: FAQEntry
    score: float
    matched_keywords: tuple[str, ...]


class FAQEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=100)
    question: dict[Language, str]
    keywords: dict[Language, list[str]]
    answer: dict[Language, str]
    fictional_demo: bool = True
    version: str = Field(min_length=1, max_length=64)


class FAQCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[FAQEntry] = Field(min_length=1)

    @classmethod
    def from_file(cls, path: str | Path) -> FAQCatalog:
        with Path(path).open(encoding="utf-8") as file:
            payload: object = json.load(file)
        if isinstance(payload, dict):
            payload_mapping = cast(dict[str, Any], payload)
            payload = payload_mapping.get("entries", payload_mapping.get("faq"))
        if not isinstance(payload, list):
            raise ValueError("FAQ data must be a list or an object containing entries")
        return cls(entries=cast(list[FAQEntry], payload))

    def retrieve(self, question: str, language: Language) -> FAQMatch | None:
        """Find the strongest configured bilingual hit without semantic guessing.

        Matching is token/phrase based, accent insensitive, and considers both
        language keyword sets.  This means a code-switched question such as
        ``"¿What schedules hay?"`` still gets the configured answer in the
        requested language.  A tie is resolved by catalogue order, which makes
        the result reproducible and auditable.
        """

        query_tokens = _tokens(question[:1_000])
        if not query_tokens:
            return None
        query_set = set(query_tokens)
        best: FAQMatch | None = None
        for entry in self.entries:
            matched: list[str] = []
            score = 0.0
            for keyword_language in (
                language,
                *[item for item in Language if item is not language],
            ):
                keywords = entry.keywords.get(keyword_language)
                if keywords is None:
                    continue
                for keyword in keywords:
                    phrase = _tokens(keyword)
                    if not phrase:
                        continue
                    phrase_text = " ".join(phrase)
                    query_text = " ".join(query_tokens)
                    first_word = next(iter(phrase), None)
                    if first_word is None:
                        continue
                    phrase_hit = (
                        phrase_text in query_text
                        if len(phrase) > 1
                        else _word_hit(first_word, query_set)
                    )
                    if phrase_hit:
                        matched.append(keyword)
                        # Phrases are more informative than a generic one-word
                        # hit.  Cap each keyword once to avoid repeated terms.
                        score += float(len(phrase) * len(phrase))
            if score <= 0:
                continue
            candidate = FAQMatch(
                entry=entry, score=score, matched_keywords=tuple(dict.fromkeys(matched))
            )
            if best is None or candidate.score > best.score:
                best = candidate
        return best

    def answer(self, question: str, language: Language) -> str | None:
        """Return a configured answer, or ``None`` for unsupported questions."""

        match = self.retrieve(question, language)
        if match is None:
            return None
        return match.entry.answer.get(language) or match.entry.answer.get(Language.EN)
