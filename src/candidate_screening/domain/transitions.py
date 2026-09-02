"""Allowed screening-status transitions."""

from .enums import ScreeningStatus

_ALLOWED: dict[ScreeningStatus, frozenset[ScreeningStatus]] = {
    ScreeningStatus.IN_PROGRESS: frozenset(
        {
            ScreeningStatus.IN_PROGRESS,
            ScreeningStatus.QUALIFIED,
            ScreeningStatus.DISQUALIFIED,
            ScreeningStatus.NEEDS_REVIEW,
            ScreeningStatus.ABANDONED,
        }
    ),
    ScreeningStatus.NEEDS_REVIEW: frozenset(
        {
            ScreeningStatus.IN_PROGRESS,
            ScreeningStatus.QUALIFIED,
            ScreeningStatus.DISQUALIFIED,
            ScreeningStatus.NEEDS_REVIEW,
            ScreeningStatus.ABANDONED,
        }
    ),
    ScreeningStatus.QUALIFIED: frozenset({ScreeningStatus.QUALIFIED}),
    ScreeningStatus.DISQUALIFIED: frozenset({ScreeningStatus.DISQUALIFIED}),
    ScreeningStatus.ABANDONED: frozenset({ScreeningStatus.ABANDONED}),
}


def can_transition(current: ScreeningStatus, target: ScreeningStatus) -> bool:
    return target in _ALLOWED[current]


def assert_transition(current: ScreeningStatus, target: ScreeningStatus) -> None:
    if not can_transition(current, target):
        raise ValueError(f"invalid screening transition: {current.value} -> {target.value}")
