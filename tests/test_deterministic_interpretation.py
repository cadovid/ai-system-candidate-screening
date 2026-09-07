from __future__ import annotations

import pytest

from candidate_screening.ai.interpreter import InterpreterResult
from candidate_screening.ai.schemas import ExtractedLocation, ExtractedValue, TurnInterpretation
from candidate_screening.application.deterministic_interpretation import (
    interpret_deterministically,
    recover_location_answer,
)
from candidate_screening.domain.enums import (
    AvailabilityType,
    Language,
    LocationMatchStatus,
    SchedulePreference,
    ScreeningField,
)
from candidate_screening.domain.models import LocationState, PendingConfirmation, ScreeningState
from candidate_screening.domain.service_areas import ServiceAreaMatcher


@pytest.mark.parametrize(
    "message",
    [
        "Puedo trabajar a tiempo completo",
        "Can work weekends",
        "¿Puedo trabajar los fines de semana?",
        "Can I work weekends?",
        "Can I work weekends",
        "What schedules are available",
    ],
)
def test_semantic_questions_and_ability_phrases_remain_model_owned(message: str) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.AVAILABILITY}
    )

    result = interpret_deterministically(state, message)

    assert result is None


@pytest.mark.parametrize("message", ["Maria Garcia", "Laura Pineda"])
def test_conservative_bare_full_name_is_a_deterministic_fallback_candidate(
    message: str,
) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.FULL_NAME}
    )

    result = interpret_deterministically(state, message)

    assert result is not None
    assert result.interpretation.full_name is not None
    assert result.interpretation.full_name.value == message
    assert result.usage["deterministic_reason"] == "name_answer"


def test_lowercase_bare_name_stays_on_the_semantic_path() -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.FULL_NAME}
    )

    assert interpret_deterministically(state, "laura pineda") is None


def test_start_availability_can_be_retained_verbatim_after_empty_model_output() -> None:
    state = ScreeningState.empty(Language.ES).model_copy(
        update={"current_field": ScreeningField.START_AVAILABILITY}
    )

    result = interpret_deterministically(state, "Pues lo antes posible por favor")

    assert result is not None
    start = result.interpretation.start_availability
    assert start is not None
    assert start.provided is True
    assert start.raw_value == "Pues lo antes posible por favor"
    assert start.precision == "unknown"
    assert result.usage["deterministic_reason"] == "start_availability_verbatim"


@pytest.mark.parametrize(("message", "complete"), [("No", True), ("Sí", False)])
def test_post_screening_faq_control_is_completed_only_when_candidate_has_no_questions(
    message: str,
    complete: bool,
) -> None:
    state = ScreeningState.empty(Language.ES).model_copy(
        update={
            "current_field": None,
            "candidate_confirmed": True,
            "faq_offer_made": True,
        }
    )

    result = interpret_deterministically(state, message)

    assert result is not None
    assert result.interpretation.faq_complete is complete
    assert result.usage["deterministic_reason"] == "faq_completion_control"


@pytest.mark.parametrize("message", ["Sí—", "si!!!", "NO...", "nope;"])
def test_control_replies_normalize_accents_punctuation_and_hyphens(message: str) -> None:
    state = ScreeningState.empty(Language.ES).model_copy(
        update={"current_field": ScreeningField.DRIVERS_LICENSE}
    )

    result = interpret_deterministically(state, message)

    assert result is not None
    assert result.interpretation.drivers_license is not None
    assert result.interpretation.drivers_license.value is ("no" not in message.casefold())


def test_pending_confirmation_accepts_only_an_exact_normalized_control() -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={
            "pending_confirmation": PendingConfirmation(
                field=ScreeningField.FULL_NAME,
                proposed_value={"value": "Ada Lovelace"},
            )
        }
    )

    assert interpret_deterministically(state, "go-ahead") is None
    result = interpret_deterministically(state, "YES!!!")

    assert result is not None
    assert result.interpretation.confirmation is True


@pytest.mark.parametrize(
    ("message", "language"),
    [
        ("Please continue in English", Language.EN),
        ("Por favor, continúa en inglés", Language.EN),
        ("en español", Language.ES),
    ],
)
def test_explicit_language_commands_are_closed_controls(message: str, language: Language) -> None:
    result = interpret_deterministically(ScreeningState.empty(Language.EN), message)

    assert result is not None
    assert result.interpretation.explicit_language is language


@pytest.mark.parametrize(
    "message",
    [
        "I have one",
        "Yep",
        "Name: Ada Lovelace; licence: yes",
        "Actually, my full name is Ada Lovelace",
        "Yes, but I need to correct that",
    ],
)
def test_natural_compound_and_correction_turns_remain_model_owned(message: str) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.DELIVERY_EXPERIENCE}
    )

    assert interpret_deterministically(state, message) is None


@pytest.mark.parametrize("message", ["Let's go", "lets go!"])
def test_clear_disclosure_start_controls_are_deterministic_fallback_candidates(
    message: str,
) -> None:
    result = interpret_deterministically(ScreeningState.empty(Language.EN), message)

    assert result is not None
    assert result.interpretation.disclosure_acknowledged is True
    assert result.usage["deterministic_reason"] == "disclosure"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Full-time", AvailabilityType.FULL_TIME),
        ("part time", AvailabilityType.PART_TIME),
        ("Jornada completa", AvailabilityType.FULL_TIME),
        ("fines de semana", AvailabilityType.WEEKENDS),
    ],
)
def test_exact_availability_options_are_safe_deterministic_fallbacks(
    message: str, expected: AvailabilityType
) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.AVAILABILITY}
    )

    result = interpret_deterministically(state, message)

    assert result is not None
    assert result.interpretation.availability is not None
    assert result.interpretation.availability.value == [expected]
    assert result.usage["deterministic_reason"] == "availability_control"


def test_exact_schedule_option_is_a_safe_deterministic_fallback() -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.PREFERRED_SCHEDULE}
    )

    result = interpret_deterministically(state, "Evening")

    assert result is not None
    assert result.interpretation.preferred_schedule is not None
    assert result.interpretation.preferred_schedule.value is SchedulePreference.EVENING


@pytest.mark.parametrize("message", ["Stop", "parar!"])
def test_exact_opt_out_control_is_provider_independent(message: str) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.AVAILABILITY}
    )

    result = interpret_deterministically(state, message)

    assert result is not None
    assert result.interpretation.opt_out_requested is True
    assert result.interpretation.intent.value == "opt_out"


@pytest.mark.parametrize(
    "message",
    ["I am looking for full-time", "I told you I can go with full-time"],
)
def test_natural_availability_prose_remains_model_owned(message: str) -> None:
    state = ScreeningState.empty(Language.EN).model_copy(
        update={"current_field": ScreeningField.AVAILABILITY}
    )

    assert interpret_deterministically(state, message) is None


@pytest.mark.parametrize("provided", [True, False])
def test_recover_location_discards_stale_patch_from_bounded_history(
    service_area_matcher: ServiceAreaMatcher,
    provided: bool,
) -> None:
    """A stale location echo cannot overwrite a different current answer."""

    state = ScreeningState.empty(Language.EN).model_copy(
        update={
            "current_field": ScreeningField.AVAILABILITY,
            "location": LocationState(
                raw_value="Madrid centro",
                normalized_value="madrid centro",
                city="Madrid",
                service_area_id="es-mad-centro",
                matched_name="Madrid — Centro",
                match_status=LocationMatchStatus.EXACT,
                confirmed=True,
            ),
        }
    )
    interpreted = InterpreterResult(
        interpretation=TurnInterpretation(
            availability=ExtractedValue(
                value=[AvailabilityType.WEEKENDS],
                provided=True,
                evidence="only weekends please",
            ),
            # This is the shape observed when a model echoes a previous
            # location while answering the availability question.
            location=ExtractedLocation(
                raw_value="Centro",
                city="Madrid",
                zone="Centro",
                provided=provided,
                evidence="Centro",
            ),
        )
    )

    recovered = recover_location_answer(
        state,
        "only weekends please",
        interpreted,
        service_area_matcher=service_area_matcher,
    )

    assert recovered.interpretation.location is None
    assert recovered.interpretation.availability is not None
    assert recovered.interpretation.availability.value == [AvailabilityType.WEEKENDS]
    assert recovered.usage["location_patch_discarded"] is True
    assert recovered.usage["location_patch_discard_reason"] == "not_grounded_in_current_message"


def test_recover_location_grounds_conversational_zone_in_pending_city(
    service_area_matcher: ServiceAreaMatcher,
) -> None:
    """A natural zone reply is resolved using the already trusted city offer."""

    state = ScreeningState.empty(Language.EN).model_copy(
        update={
            "current_field": ScreeningField.LOCATION,
            "location": LocationState(
                raw_value="Madrid",
                normalized_value="madrid",
                city="Madrid",
                match_status=LocationMatchStatus.AMBIGUOUS,
                suggestion_ids=["es-mad-centro", "es-mad-salamanca"],
            ),
            "pending_confirmation": PendingConfirmation(
                field=ScreeningField.LOCATION,
                proposed_value={
                    "city": "Madrid",
                    "service_area_ids": ["es-mad-centro", "es-mad-salamanca"],
                },
                reason="service_area_city",
            ),
        }
    )
    interpreted = InterpreterResult(
        interpretation=TurnInterpretation(
            location=ExtractedLocation(
                raw_value="Centro is fine",
                city="Madrid",
                zone="Centro",
                provided=True,
                evidence="Centro is fine",
            )
        )
    )

    recovered = recover_location_answer(
        state,
        "Centro is fine",
        interpreted,
        service_area_matcher=service_area_matcher,
    )

    assert recovered.interpretation.location is not None
    assert recovered.interpretation.location.city == "Madrid"
    assert recovered.interpretation.location.zone == "Centro"
    assert recovered.usage["deterministic_fallback_reason"] in {
        "model_location_zone_context",
        "pending_city_zone_context",
    }
