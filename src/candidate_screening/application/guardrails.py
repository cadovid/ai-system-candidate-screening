"""Deterministic safety checks around model and persistence boundaries.

The language model is useful for extracting facts, but it is not a security
boundary.  This module contains the small, conservative checks that run before
model calls and before a generated recruiter summary is shown.  They are
intentionally independent of any provider so that the same controls apply in
tests, local development, and production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# These phrases are deliberately broad enough to catch common jailbreaks, but
# the action is only to keep the message out of the model and return a safe,
# task-focused response.  No candidate data is used to make an eligibility
# decision here.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,80}\b(previous|system|developer|instructions?|rules?)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(reveal|show|print|dump|repeat)\b.{0,80}\b(system prompt|hidden prompt|instructions?|secrets?|api key)\b",
        re.I | re.S,
    ),
    re.compile(r"\b(jailbreak|prompt injection|do anything now|dan mode)\b", re.I),
    re.compile(r"\b(system message|developer message|tool call|function call)\b", re.I),
)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{7,}\d)(?!\w)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
_SECRET_RE = re.compile(
    r"\b(password|contraseña|passwd|api[ _-]?key|token|secret|cvv|cvc|iban|social security|nif|nie|curp|rfc)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """Outcome of inspecting one untrusted candidate message."""

    message: str
    prompt_injection: bool = False
    sensitive_data: bool = False

    @property
    def blocked(self) -> bool:
        return self.prompt_injection or self.sensitive_data


def _redact(message: str) -> tuple[str, bool]:
    """Redact high-risk contact/credential-like values before persistence."""

    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        found = True
        return "[redacted]"

    redacted = _EMAIL_RE.sub(replace, message)
    redacted = _PHONE_RE.sub(replace, redacted)
    # Do not redact every ordinary number: this rule only catches long,
    # card-shaped digit sequences and therefore leaves years/addresses useful.
    redacted = _CARD_RE.sub(replace, redacted)
    if _SECRET_RE.search(message):
        found = True
        redacted = re.sub(_SECRET_RE, "[sensitive term]", redacted)
    return redacted, found


def inspect_message(message: str) -> GuardrailResult:
    """Inspect and minimally redact candidate text without making decisions."""

    content = message.strip()
    injection = any(pattern.search(content) for pattern in _INJECTION_PATTERNS)
    redacted, sensitive = _redact(content)
    return GuardrailResult(
        message=redacted[:2_000],
        prompt_injection=injection,
        sensitive_data=sensitive,
    )


def summary_is_safe(summary: str) -> bool:
    """Reject provider output that looks like a prompt leak or data invention."""

    value = " ".join(summary.split())
    if not value or len(value) > 1_200:
        return False
    if any(pattern.search(value) for pattern in _INJECTION_PATTERNS):
        return False
    if _EMAIL_RE.search(value) or _PHONE_RE.search(value) or _CARD_RE.search(value):
        return False
    return not re.search(
        r"\b(password|api[ _-]?key|secret|prompt|protected characteristic|race|gender|religion)\b",
        value,
        re.I,
    )


__all__ = ["GuardrailResult", "inspect_message", "summary_is_safe"]
