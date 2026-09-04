from __future__ import annotations

import pytest

from candidate_screening.ai.schemas import TurnIntent
from candidate_screening.application.deterministic_interpretation import (
    interpret_deterministically,
)
from candidate_screening.domain.enums import Language, ScreeningField
from candidate_screening.domain.models import ScreeningState


@pytest.mark.parametrize(
    ("message", "is_question"),
    [
        ("Puedo trabajar a tiempo completo", False),
        ("Can work weekends", False),
        ("¿Puedo trabajar los fines de semana?", True),
        ("Can I work weekends?", True),
        ("Can I work weekends", True),
        ("What schedules are available", True),
    ],
)
def test_question_detection_keeps_ability_answers_model_owned(
    message: str,
    is_question: bool,
) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.AVAILABILITY}
    )

    result = interpret_deterministically(state, message)

    assert (
        result is not None and result.interpretation.intent is TurnIntent.QUESTION
    ) is is_question
    if not is_question:
        assert result is None
