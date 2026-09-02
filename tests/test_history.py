from __future__ import annotations

from typing import cast

from pydantic_ai.messages import ModelRequest, UserPromptPart

from candidate_screening.ai.history import HistoryMessage, build_bounded_history


def test_history_is_bounded_and_keeps_newest_messages() -> None:
    messages = [
        HistoryMessage("user", "old user"),
        HistoryMessage("assistant", "old assistant"),
        HistoryMessage("user", "new user"),
        HistoryMessage("assistant", "new assistant"),
    ]
    history = build_bounded_history(messages, max_pairs=1, max_characters=30)
    assert len(history) == 2
    assert "new user" in str(history[0])
    assert "new assistant" in str(history[1])


def test_history_truncates_one_oversized_latest_message() -> None:
    history = build_bounded_history(
        [HistoryMessage("user", "x" * 100)], max_pairs=1, max_characters=12
    )
    assert len(history) == 1
    request = cast(ModelRequest, history[0])
    part = cast(UserPromptPart, request.parts[0])
    content = part.content
    assert isinstance(content, str)
    assert len(content) == 12
