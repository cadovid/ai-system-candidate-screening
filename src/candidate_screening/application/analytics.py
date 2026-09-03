"""Small, deterministic analytics primitives.

The screening workflow already persists :class:`AuditEventORM` rows.  This
module deliberately keeps the reporting shape independent from SQLAlchemy so
that workers, tests, and offline jobs can aggregate the same events without
opening a database connection.  It is not intended to be a product analytics
or candidate-ranking system: values are operational counters only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast


class AnalyticsEventType:
    """Stable event names used by the operational audit stream."""

    CONVERSATION_STARTED = "conversation_started"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    SCREENING_QUALIFIED = "screening_qualified"
    SCREENING_DISQUALIFIED = "screening_disqualified"
    SCREENING_NEEDS_REVIEW = "screening_needs_review"
    SCREENING_ABANDONED = "screening_abandoned"
    CANDIDATE_OPTED_OUT = "candidate_opted_out"
    REENGAGEMENT_SENT = "reengagement_sent"
    REENGAGEMENT_SUPPRESSED = "reengagement_suppressed"
    SUMMARY_GENERATED = "summary_generated"
    SUMMARY_FALLBACK = "summary_fallback"
    RECRUITER_REVIEW_RECORDED = "recruiter_review_recorded"
    FAQ_ANSWERED = "faq_answered"


@dataclass(frozen=True, slots=True)
class AnalyticsEvent:
    """A transport-neutral representation of one audit event."""

    event_type: str
    created_at: datetime | None = None
    screening_session_id: str | None = None
    turn_id: str | None = None
    stage: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())

    @classmethod
    def from_row(cls, row: Any) -> AnalyticsEvent:
        """Convert an ORM row or a mapping without leaking ORM details."""

        if isinstance(row, cls):
            return row
        if isinstance(row, Mapping):
            row_mapping = cast(Mapping[str, Any], row)
            metadata = row_mapping.get("metadata", row_mapping.get("event_metadata", {}))
            return cls(
                event_type=str(row_mapping.get("event_type", "")),
                created_at=row_mapping.get("created_at"),
                screening_session_id=row_mapping.get("screening_session_id"),
                turn_id=row_mapping.get("turn_id"),
                stage=row_mapping.get("stage"),
                metadata=(
                    dict(cast(Mapping[str, Any], metadata)) if isinstance(metadata, Mapping) else {}
                ),
            )
        return cls(
            event_type=str(getattr(row, "event_type", "")),
            created_at=getattr(row, "created_at", None),
            screening_session_id=getattr(row, "screening_session_id", None),
            turn_id=getattr(row, "turn_id", None),
            stage=getattr(row, "stage", None),
            metadata=dict(getattr(row, "event_metadata", {}) or {}),
        )


@dataclass(frozen=True, slots=True)
class AnalyticsAggregate:
    """Operational counters derived from audit events.

    ``event_counts`` retains all event names, including names introduced by a
    newer producer.  The named counters are convenience fields for dashboards
    and remain safe when an event stream is incomplete.
    """

    total_events: int = 0
    event_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    screenings_started: int = 0
    turns_started: int = 0
    turns_completed: int = 0
    turns_failed: int = 0
    guardrail_blocks: int = 0
    qualified: int = 0
    disqualified: int = 0
    needs_review: int = 0
    abandoned: int = 0
    opted_out: int = 0
    reengagements_sent: int = 0
    reengagements_suppressed: int = 0
    summaries_generated: int = 0
    summaries_fallback: int = 0
    reviews_recorded: int = 0

    @property
    def reminders_sent(self) -> int:
        """Friendly alias used by operational scripts."""

        return self.reengagements_sent


@dataclass(frozen=True, slots=True)
class PersistedAnalyticsAggregate:
    """Aggregate metrics derived from persisted workflow rows.

    The function below accepts row-like values rather than SQLAlchemy models,
    keeping this reporting layer deterministic and easy to exercise with
    fixtures or an offline export.  It only reads operational metadata,
    statuses, timestamps, and counts; message content and candidate identity
    are never returned in this shape.
    """

    average_completed_messages: float | None = None
    average_completed_turns: float | None = None
    average_screening_duration_seconds: float | None = None
    language_distribution: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    faq_usage_count: int = 0
    clarification_retry_count: int = 0
    clarification_retry_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    disqualification_reason_distribution: dict[str, int] = field(
        default_factory=lambda: dict[str, int]()
    )
    dropoff_stage_distribution: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    interaction_mode_distribution: dict[str, int] = field(default_factory=lambda: dict[str, int]())


# ``abandoned`` has a ``completed_at`` timestamp in the workflow, but it is a
# drop-off rather than a completed screening and must not enter completion
# averages.  Keep the two concepts explicit instead of treating every
# terminal state as a successful completion.
_COMPLETED_STATUSES = {"qualified", "disqualified", "needs_review"}
_DROPOFF_STATUSES = {"in_progress", "abandoned"}


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        # ``Mapping`` without a type parameter narrows to ``Unknown`` under
        # Pyright even though callers intentionally accept arbitrary export
        # row shapes.  Keep the boundary dynamic and make that choice
        # explicit here.
        row_mapping = cast(Mapping[str, Any], row)
        return row_mapping.get(name, default)
    return getattr(row, name, default)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = " ".join(value.split()).strip()
        return normalized or None
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    return normalized or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _positive_int(value: Any) -> int:
    # Boolean JSON values are not retry counters, even though bool subclasses
    # int in Python.
    if isinstance(value, bool):
        return 0
    try:
        converted = int(value)
    except TypeError, ValueError:
        return 0
    return max(0, converted)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _reason_codes(value: Any) -> tuple[str, ...]:
    values: tuple[Any, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = tuple(cast(Iterable[Any], value))
    else:
        return ()
    return tuple(
        dict.fromkeys(
            normalized
            for item in values
            if (normalized := _text(item)) is not None and len(normalized) <= 100
        )
    )


def _dropoff_stage(state_value: Any) -> str | None:
    """Find the first missing/awaiting stage without inspecting evidence text."""

    state = _mapping(state_value)
    current = _text(state.get("current_field"))
    if current is not None:
        return current

    if not state:
        return None
    if not state.get("full_name"):
        return "full_name"
    if not state.get("drivers_license"):
        return "drivers_license"
    location = _mapping(state.get("location"))
    if (
        location.get("match_status") != "exact"
        or not location.get("confirmed")
        or not location.get("service_area_id")
    ):
        return "location"
    if not state.get("availability"):
        return "availability"
    if not state.get("preferred_schedule"):
        return "preferred_schedule"
    if not state.get("delivery_experience"):
        return "delivery_experience"
    if not state.get("start_availability"):
        return "start_availability"
    if state.get("pending_confirmation"):
        pending = _mapping(state.get("pending_confirmation"))
        return _text(pending.get("field")) or "confirmation"
    if not state.get("candidate_confirmed"):
        return "confirmation"
    return None


def aggregate_persisted_data(
    sessions: Iterable[Any],
    conversations: Iterable[Any],
    turns: Iterable[Any],
    messages: Iterable[Any],
    results: Iterable[Any],
    events: Iterable[AnalyticsEvent | Mapping[str, Any] | Any],
) -> PersistedAnalyticsAggregate:
    """Compute aggregate workflow metrics from persisted row-like values.

    A completed screening is one with a completed screening status
    (``qualified``, ``disqualified``, or ``needs_review``), or a non-abandoned
    session with an explicit completion timestamp.  Message and turn averages
    use that set as their denominator.  Messages mean user messages attached
    to completed turns; turns mean turns whose status is ``completed``.
    Duration averages use only completed sessions with valid non-negative
    ``started_at``/``completed_at`` pairs.  Language and drop-off
    distributions use all sessions for which the corresponding value is
    available.
    """

    session_rows = tuple(sessions)
    conversation_rows = tuple(conversations)
    turn_rows = tuple(turns)
    message_rows = tuple(messages)
    result_rows = tuple(results)
    normalized_events = tuple(AnalyticsEvent.from_row(event) for event in events)

    conversation_to_session = {
        conversation_id: session_id
        for row in conversation_rows
        if (conversation_id := _text(_row_value(row, "id"))) is not None
        if (session_id := _text(_row_value(row, "screening_session_id"))) is not None
    }
    completed_session_ids: set[str] = set()
    for row in session_rows:
        session_id = _text(_row_value(row, "id"))
        if session_id is None:
            continue
        status = _text(_row_value(row, "status"))
        completed_at = _datetime(_row_value(row, "completed_at"))
        if status != "abandoned" and (status in _COMPLETED_STATUSES or completed_at is not None):
            completed_session_ids.add(session_id)

    turn_status_by_id: dict[str, str] = {}
    completed_turns_by_session: Counter[str] = Counter()
    interaction_mode_distribution: Counter[str] = Counter()
    for row in turn_rows:
        turn_id = _text(_row_value(row, "id"))
        conversation_id = _text(_row_value(row, "conversation_id"))
        if turn_id is None or conversation_id is None:
            continue
        status = _text(_row_value(row, "status")) or ""
        interaction_mode_distribution[_text(_row_value(row, "input_mode")) or "text"] += 1
        turn_status_by_id[turn_id] = status
        session_id = conversation_to_session.get(conversation_id)
        if status == "completed" and session_id in completed_session_ids:
            completed_turns_by_session[session_id] += 1

    completed_messages_by_session: Counter[str] = Counter()
    for row in message_rows:
        if _text(_row_value(row, "direction")) != "user":
            continue
        conversation_id = _text(_row_value(row, "conversation_id"))
        session_id = conversation_to_session.get(conversation_id or "")
        if session_id not in completed_session_ids:
            continue
        turn_id = _text(_row_value(row, "turn_id"))
        # Messages not attached to a turn can come from imports or older
        # schema versions; they are still valid completed-screening messages.
        if turn_id is not None and turn_status_by_id.get(turn_id) != "completed":
            continue
        completed_messages_by_session[session_id] += 1

    def average(counter: Counter[str]) -> float | None:
        if not completed_session_ids:
            return None
        return sum(counter.get(session_id, 0) for session_id in completed_session_ids) / len(
            completed_session_ids
        )

    durations: list[float] = []
    for row in session_rows:
        session_id = _text(_row_value(row, "id"))
        if session_id not in completed_session_ids:
            continue
        started_at = _datetime(_row_value(row, "started_at"))
        completed_at = _datetime(_row_value(row, "completed_at"))
        if started_at is None or completed_at is None:
            continue
        seconds = (completed_at - started_at).total_seconds()
        if seconds >= 0:
            durations.append(seconds)

    language_distribution: Counter[str] = Counter()
    for row in session_rows:
        language_distribution[_text(_row_value(row, "preferred_language")) or "unknown"] += 1

    # A FAQ answer is persisted as a dedicated event by current producers and
    # as turn metadata for compatibility with older exports.  Deduplicate the
    # two representations when they share a turn id.
    faq_keys: set[tuple[str, str | int]] = set()
    for index, event in enumerate(normalized_events):
        if (
            event.event_type != AnalyticsEventType.FAQ_ANSWERED
            and event.metadata.get("faq_answered") is not True
        ):
            continue
        if event.turn_id is not None:
            faq_keys.add(("turn", event.turn_id))
        else:
            faq_keys.add(("event", index))

    clarification_retry_counts: Counter[str] = Counter()
    for row in session_rows:
        for field_name, raw_count in _mapping(_row_value(row, "clarification_counts")).items():
            count = _positive_int(raw_count)
            if count:
                clarification_retry_counts[_text(field_name) or "unknown"] += count

    disqualification_reason_distribution: Counter[str] = Counter()
    result_reason_sessions: set[str] = set()
    for row in result_rows:
        if _text(_row_value(row, "status")) != "disqualified":
            continue
        session_id = _text(_row_value(row, "screening_session_id"))
        reasons = _reason_codes(_row_value(row, "reason_codes"))
        if session_id is not None and session_id in result_reason_sessions:
            continue
        if session_id is not None and reasons:
            result_reason_sessions.add(session_id)
        for reason in reasons:
            disqualification_reason_distribution[reason] += 1

    audit_reason_sessions: set[str] = set()
    for event in normalized_events:
        if event.event_type != AnalyticsEventType.SCREENING_DISQUALIFIED:
            continue
        session_id = _text(event.screening_session_id)
        if session_id is not None and (session_id in result_reason_sessions):
            continue
        if session_id is not None and session_id in audit_reason_sessions:
            continue
        reasons = _reason_codes(event.metadata.get("reason_codes"))
        if session_id is not None and reasons:
            audit_reason_sessions.add(session_id)
        for reason in reasons:
            disqualification_reason_distribution[reason] += 1

    dropoff_stage_distribution: Counter[str] = Counter()
    for row in session_rows:
        status = _text(_row_value(row, "status"))
        if status not in _DROPOFF_STATUSES:
            continue
        stage = _dropoff_stage(_row_value(row, "screening_state"))
        if stage is None:
            stage = _text(_row_value(row, "current_field"))
        if stage is not None:
            dropoff_stage_distribution[stage] += 1

    return PersistedAnalyticsAggregate(
        average_completed_messages=average(completed_messages_by_session),
        average_completed_turns=average(completed_turns_by_session),
        average_screening_duration_seconds=(sum(durations) / len(durations) if durations else None),
        language_distribution=dict(sorted(language_distribution.items())),
        faq_usage_count=len(faq_keys),
        clarification_retry_count=sum(clarification_retry_counts.values()),
        clarification_retry_counts=dict(sorted(clarification_retry_counts.items())),
        disqualification_reason_distribution=dict(
            sorted(disqualification_reason_distribution.items())
        ),
        dropoff_stage_distribution=dict(sorted(dropoff_stage_distribution.items())),
        interaction_mode_distribution=dict(sorted(interaction_mode_distribution.items())),
    )


def aggregate_events(
    events: Iterable[AnalyticsEvent | Mapping[str, Any] | Any],
) -> AnalyticsAggregate:
    """Aggregate an iterable of audit rows deterministically.

    Unknown or blank event names are retained as ``""``.  This makes malformed
    rows visible in a dashboard rather than silently changing the denominator.
    """

    normalized = [AnalyticsEvent.from_row(event) for event in events]
    counts = Counter(event.event_type for event in normalized)

    def get(name: str) -> int:
        return int(counts.get(name, 0))

    return AnalyticsAggregate(
        total_events=len(normalized),
        event_counts=dict(sorted(counts.items())),
        screenings_started=get(AnalyticsEventType.CONVERSATION_STARTED),
        turns_started=get(AnalyticsEventType.TURN_STARTED),
        turns_completed=get(AnalyticsEventType.TURN_COMPLETED),
        turns_failed=get(AnalyticsEventType.TURN_FAILED),
        guardrail_blocks=get(AnalyticsEventType.GUARDRAIL_BLOCKED),
        qualified=get(AnalyticsEventType.SCREENING_QUALIFIED),
        disqualified=get(AnalyticsEventType.SCREENING_DISQUALIFIED),
        needs_review=get(AnalyticsEventType.SCREENING_NEEDS_REVIEW),
        abandoned=get(AnalyticsEventType.SCREENING_ABANDONED),
        opted_out=get(AnalyticsEventType.CANDIDATE_OPTED_OUT),
        reengagements_sent=get(AnalyticsEventType.REENGAGEMENT_SENT),
        reengagements_suppressed=get(AnalyticsEventType.REENGAGEMENT_SUPPRESSED),
        summaries_generated=get(AnalyticsEventType.SUMMARY_GENERATED),
        summaries_fallback=get(AnalyticsEventType.SUMMARY_FALLBACK),
        reviews_recorded=get(AnalyticsEventType.RECRUITER_REVIEW_RECORDED),
    )


# Common names used by command-line/reporting adapters.
compute_aggregate = aggregate_events
aggregate_analytics = aggregate_events


__all__ = [
    "AnalyticsAggregate",
    "AnalyticsEvent",
    "AnalyticsEventType",
    "PersistedAnalyticsAggregate",
    "aggregate_analytics",
    "aggregate_events",
    "aggregate_persisted_data",
    "compute_aggregate",
]
