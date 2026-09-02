"""Bounded conversion of persisted chat messages to model messages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime | None = None


def build_bounded_history(
    messages: Sequence[HistoryMessage], *, max_pairs: int = 6, max_characters: int = 8_000
) -> list[ModelMessage]:
    """Build server-owned bounded history; clients never submit model messages."""

    if max_pairs < 1 or max_characters < 1:
        raise ValueError("history bounds must be positive")
    selected: list[HistoryMessage] = []
    characters = 0
    pairs = 0
    for message in reversed(messages):
        content = message.content.strip()
        if not content:
            continue
        remaining = max_characters - characters
        if remaining <= 0:
            break
        # Keep the newest content even when one persisted message is larger
        # than the entire history budget.  This gives a hard bound while
        # retaining the latest conversational context.
        bounded_content = content[:remaining]
        selected.append(HistoryMessage(message.role, bounded_content, message.created_at))
        characters += len(bounded_content)
        if message.role == "user":
            pairs += 1
            if pairs >= max_pairs:
                break
    selected.reverse()

    result: list[ModelMessage] = []
    for message in selected:
        timestamp = message.created_at or datetime.now(UTC)
        if message.role == "user":
            result.append(
                ModelRequest([UserPromptPart(content=message.content)], timestamp=timestamp)
            )
        else:
            result.append(ModelResponse([TextPart(content=message.content)], timestamp=timestamp))
    return result
