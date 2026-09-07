"""Deterministic response intents for the candidate conversation.

The screening decision and the candidate-facing copy are deliberately
separate concerns.  ``ResponsePlan`` is the small, deterministic boundary
between them: the controller chooses a plan from canonical state and the
localized renderer turns that plan into bounded text.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import Any, Protocol

from candidate_screening.domain.enums import Language, ScreeningField
from candidate_screening.domain.models import PendingConfirmation, ScreeningDecision, ScreeningState


class ResponseKind(StrEnum):
    """Closed set of candidate-facing response intents."""

    PROMPT = "prompt"
    CLARIFICATION = "clarification"
    PENDING_CONFIRMATION = "pending_confirmation"
    FAQ_BRIDGE = "faq_bridge"
    FAQ_OFFER = "faq_offer"
    FAQ_FOLLOWUP = "faq_followup"
    UNKNOWN_QUESTION = "unknown_question"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    NEEDS_REVIEW = "needs_review"
    OPT_OUT = "opt_out"
    SECURITY = "security"
    SENSITIVE = "sensitive"
    TEMPORARY_FAILURE = "temporary_failure"
    MAX_TURNS = "max_turns"
    FINAL_CONFIRMATION = "final_confirmation"


class VariantSelector(Protocol):
    """Injectable variant selector used by deterministic response rendering."""

    def __call__(self, key: str, variant_count: int) -> int: ...


def stable_variant_index(key: str, variant_count: int) -> int:
    """Select a variant from a stable SHA-256 digest.

    Python's built-in ``hash`` is intentionally process-randomized.  Response
    variants must be reproducible across workers, replays, and deployments,
    so only the UTF-8 key and SHA-256 are used here.
    """

    if variant_count < 1:
        raise ValueError("variant_count must be positive")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False) % variant_count


def random_variant_index(key: str, variant_count: int) -> int:
    """Select a copy variant at runtime without affecting domain behavior.

    ``key`` is accepted to satisfy :class:`VariantSelector`; copy selection is
    intentionally non-deterministic in the live UI. Tests inject their own
    selector, and the selected response is persisted for exact replay.
    """

    del key
    if variant_count < 1:
        raise ValueError("variant_count must be positive")
    return secrets.randbelow(variant_count)


def choose_variant(
    key: str,
    variants: Sequence[str],
    *,
    selector: VariantSelector | Callable[[str], int] | None = None,
) -> str:
    """Choose one bounded variant deterministically or through an injection."""

    if not variants:
        raise ValueError("variants must not be empty")
    if selector is None:
        index = stable_variant_index(key, len(variants))
    else:
        try:
            index = selector(key, len(variants))  # type: ignore[call-arg]
        except TypeError:
            # A one-argument callable is convenient in tests and for simple
            # feature-flag adapters.  Keep the public protocol two-argument
            # while accepting that narrower injection safely.
            index = selector(key)  # type: ignore[call-arg]
        if not isinstance(index, int):
            raise TypeError("variant selector must return an integer")
        index %= len(variants)
    return variants[index]


@dataclass(frozen=True, slots=True)
class ResponsePlan:
    """A complete, localized response intent chosen without model text."""

    kind: ResponseKind | str
    language: Language
    field: ScreeningField | None = None
    pending: PendingConfirmation | None = None
    decision: ScreeningDecision | None = None
    state: ScreeningState | None = None
    faq_answer: str | None = None
    variant_key: str = ""
    context: Mapping[str, Any] = dataclass_field(default_factory=lambda: dict[str, Any]())

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResponseKind):
            object.__setattr__(self, "kind", ResponseKind(self.kind))

    @property
    def is_terminal(self) -> bool:
        return self.kind in {
            ResponseKind.QUALIFIED,
            ResponseKind.DISQUALIFIED,
            ResponseKind.NEEDS_REVIEW,
            ResponseKind.OPT_OUT,
            ResponseKind.MAX_TURNS,
        }

    @property
    def variant_seed(self) -> str:
        """Return a stable seed even when callers omit an explicit key."""

        kind = self.kind if isinstance(self.kind, ResponseKind) else ResponseKind(self.kind)
        return self.variant_key or ":".join(
            part
            for part in (
                kind.value,
                self.language.value,
                self.field.value if self.field else "",
            )
            if part
        )


__all__ = [
    "ResponseKind",
    "ResponsePlan",
    "VariantSelector",
    "choose_variant",
    "stable_variant_index",
]
