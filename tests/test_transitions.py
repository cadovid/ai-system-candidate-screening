from __future__ import annotations

import pytest

from candidate_screening.domain.enums import ScreeningStatus
from candidate_screening.domain.transitions import assert_transition, can_transition


def test_terminal_statuses_are_sticky_and_active_statuses_can_progress() -> None:
    statuses = tuple(ScreeningStatus)
    for current in statuses:
        for target in statuses:
            expected = (
                target is current
                if current
                in {
                    ScreeningStatus.QUALIFIED,
                    ScreeningStatus.DISQUALIFIED,
                    ScreeningStatus.ABANDONED,
                }
                else True
            )
            assert can_transition(current, target) is expected
            if expected:
                assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ScreeningStatus.QUALIFIED, ScreeningStatus.IN_PROGRESS),
        (ScreeningStatus.DISQUALIFIED, ScreeningStatus.QUALIFIED),
        (ScreeningStatus.ABANDONED, ScreeningStatus.NEEDS_REVIEW),
    ],
)
def test_invalid_terminal_transitions_raise(
    current: ScreeningStatus, target: ScreeningStatus
) -> None:
    assert can_transition(current, target) is False
    with pytest.raises(ValueError, match="invalid screening transition"):
        assert_transition(current, target)
