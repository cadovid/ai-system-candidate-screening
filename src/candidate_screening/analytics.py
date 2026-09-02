"""Public import alias for operational analytics primitives."""

from .application.analytics import (
    AnalyticsAggregate,
    AnalyticsEvent,
    AnalyticsEventType,
    PersistedAnalyticsAggregate,
    aggregate_analytics,
    aggregate_events,
    aggregate_persisted_data,
    compute_aggregate,
)

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
