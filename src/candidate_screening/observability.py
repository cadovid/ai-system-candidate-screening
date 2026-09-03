"""Small, dependency-free operational logging helpers.

Application logs are a useful debugging surface, but they must not become a
second candidate-data store.  The formatter below emits one JSON object per
line, keeps an intentionally small allow-list of structured fields, hashes
identifiers, and strips common contact/credential patterns from messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, cast

from .application.guardrails import inspect_message

_SAFE_FIELDS = frozenset(
    {
        "check",
        "code",
        "decision_status",
        "error_type",
        "event",
        "http_method",
        "http_path",
        "latency_ms",
        "llm_provider",
        "model_name",
        "provider_error_category",
        "reason_codes",
        "required_environment_variable",
        "security_event",
        "status_code",
        "summary_status",
        "turn_number",
    }
)
_IDENTIFIER_FIELDS = frozenset(
    {
        "conversation_id",
        "idempotency_key",
        "reviewer_id",
        "screening_session_id",
        "session_id",
        "turn_id",
    }
)
_SECRET_VALUE_RE = re.compile(
    r"(?:\[sensitive term\]|\b(?:password|passwd|api[ _-]?key|token|secret|cvv|cvc|iban)\b)"
    r"(?:\s*(?:is|=|:)\s*|\s+)[^\s,;]+",
    re.IGNORECASE,
)


def _hash_identifier(value: object) -> str:
    """Return a short, non-reversible representation for log correlation."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


class PiiSafeJsonFormatter(logging.Formatter):
    """Format records as JSON without copying exception text or raw extras."""

    def format(self, record: logging.LogRecord) -> str:
        # The guardrail's redaction is intentionally reused at the final log
        # boundary so a future call site cannot accidentally log a raw email,
        # phone number, card-shaped value, or credential term.
        safe_message = inspect_message(record.getMessage()).message
        safe_message = _SECRET_VALUE_RE.sub("[redacted]", safe_message)[:500]
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": safe_message,
        }
        for key in _SAFE_FIELDS:
            if key not in record.__dict__:
                continue
            value = record.__dict__[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
            elif isinstance(value, (list, tuple)):
                items = cast(list[Any] | tuple[Any, ...], value)
                payload[key] = [str(item)[:100] for item in items[:20]]
        for key in _IDENTIFIER_FIELDS:
            if key in record.__dict__ and record.__dict__[key] is not None:
                payload[key] = _hash_identifier(record.__dict__[key])
        if record.exc_info is not None:
            exception_type = record.exc_info[0]
            if exception_type is not None:
                payload["exception_type"] = exception_type.__name__
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _level_number(level: str) -> int:
    value = getattr(logging, str(level).strip().upper(), logging.INFO)
    return value if isinstance(value, int) else logging.INFO


def configure_logging(level: str = "INFO") -> None:
    """Configure the process logger once, honoring the configured level."""

    root = logging.getLogger()
    numeric_level = _level_number(level)
    root.setLevel(numeric_level)
    for handler in root.handlers:
        handler.setLevel(numeric_level)
        if getattr(handler, "_candidate_screening_json", False):
            handler.setFormatter(PiiSafeJsonFormatter())
    if not any(getattr(handler, "_candidate_screening_json", False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(numeric_level)
        handler.setFormatter(PiiSafeJsonFormatter())
        cast(Any, handler)._candidate_screening_json = True
        root.addHandler(handler)


__all__ = ["PiiSafeJsonFormatter", "configure_logging"]
