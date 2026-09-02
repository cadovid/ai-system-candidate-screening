"""Stable domain enumerations.

These values are persisted and may appear in API responses, so changing one is
a migration/API compatibility decision rather than a presentation-only edit.
"""

from enum import StrEnum


class Language(StrEnum):
    ES = "es"
    EN = "en"


class AvailabilityType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    WEEKENDS = "weekends"


class SchedulePreference(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    FLEXIBLE = "flexible"


class ScreeningField(StrEnum):
    FULL_NAME = "full_name"
    DRIVERS_LICENSE = "drivers_license"
    LOCATION = "location"
    AVAILABILITY = "availability"
    PREFERRED_SCHEDULE = "preferred_schedule"
    DELIVERY_EXPERIENCE = "delivery_experience"
    START_AVAILABILITY = "start_availability"


class ScreeningStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    NEEDS_REVIEW = "needs_review"
    ABANDONED = "abandoned"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    OPTED_OUT = "opted_out"


class TurnStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class LocationMatchStatus(StrEnum):
    UNRESOLVED = "unresolved"
    EXACT = "exact"
    NEEDS_CONFIRMATION = "needs_confirmation"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class DisqualificationReason(StrEnum):
    NO_DRIVERS_LICENSE = "no_drivers_license"
    OUTSIDE_SERVICE_AREA = "outside_service_area"


class ReviewReason(StrEnum):
    AMBIGUOUS_LOCATION = "ambiguous_location"
    RETRY_LIMIT = "retry_limit"
    CONTRADICTORY_CRITICAL_DATA = "contradictory_critical_data"
    MODEL_INTERPRETATION_UNCERTAIN = "model_interpretation_uncertain"


class ValidationSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"
