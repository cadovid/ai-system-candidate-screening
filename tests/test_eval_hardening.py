# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from candidate_screening.ai.interpreter import InterpreterDependencies, InterpreterResult
from candidate_screening.domain.enums import Language, ScreeningField, ScreeningStatus
from candidate_screening.domain.models import ScreeningState
from candidate_screening.evals import (
    EvalCase,
    EvalScenario,
    _check_result,
    _deterministic_interpretation,
    _fields_match,
    _value_matches,
    load_eval_cases,
    load_scenarios,
    run_deterministic,
    run_live,
)


def test_fixture_catalog_has_meaningful_bilingual_security_and_resilience_cases() -> None:
    scenarios = load_scenarios()
    cases = load_eval_cases()
    assert len(scenarios) >= 15
    assert len(cases) >= 15
    assert {
        "multi_field_first_answer",
        "language_switch_es_to_en",
        "code_switching",
    } <= scenarios.keys()
    assert {"prompt_injection", "provider_failure", "malformed_output"} <= scenarios.keys()
    assert {
        "faq_then_answer",
        "off_topic_then_answer",
        "correction_requires_confirmation",
    } <= scenarios.keys()
    assert {case.scenario for case in cases} == set(scenarios)


def test_deterministic_report_asserts_reason_codes_state_language_and_interaction_outcomes() -> (
    None
):
    report = run_deterministic(load_eval_cases(), load_scenarios())

    assert report.failed == 0
    assert report.total >= 15
    by_id = {result.id: result for result in report.results}
    assert by_id["multi_field_first_answer"].canonical_fields["full_name"] == "Isla Morgan"
    assert by_id["multi_field_first_answer"].reason_codes == ("all_explicit_criteria_met",)
    assert by_id["multi_field_first_answer"].language is Language.EN
    assert by_id["faq_then_answer"].faq_answered is True
    assert by_id["faq_then_answer"].faq_answers
    assert by_id["correction_requires_confirmation"].correction_fields == ("full_name",)
    assert by_id["correction_requires_confirmation"].correction_confirmed is True
    assert by_id["language_switch_es_to_en"].language_trace == (Language.ES, Language.EN)
    assert by_id["prompt_injection"].security_event is True
    assert by_id["provider_failure"].retryable is True
    assert by_id["malformed_output"].retryable is True
    assert by_id["duplicate_answer"].canonical_fields["pending_confirmation"] is None


def test_fixture_interpreter_handles_pending_confirmation_and_multi_field_patches() -> None:
    state = ScreeningState.empty(Language.EN)
    first = _deterministic_interpretation(
        "Name: Ada Lovelace; licence: yes; location: Madrid centro; availability: full time; "
        "schedule: morning; experience: 3 years Glovo; start: ASAP",
        state,
    )
    assert first.full_name is not None and first.full_name.value == "Ada Lovelace"
    assert first.drivers_license is not None and first.drivers_license.value is True
    assert first.location is not None and first.location.raw_value == "Madrid centro"
    assert first.availability is not None
    assert first.preferred_schedule is not None
    assert first.delivery_experience is not None and first.delivery_experience.years == 3
    assert first.start_availability is not None

    # Use a real pending object so this branch mirrors the controller's state.
    from candidate_screening.domain.models import PendingConfirmation

    state.pending_confirmation = PendingConfirmation(
        field=ScreeningField.FULL_NAME, proposed_value={"value": "Bea"}
    )
    confirmation = _deterministic_interpretation("yes", state)
    assert confirmation.confirmation is True
    assert confirmation.full_name is None

    correction = _deterministic_interpretation("Actually, my full name is Bea", state)
    assert correction.full_name is not None
    assert correction.full_name.correction is True
    assert correction.full_name.value == "Bea"


def test_eval_case_parses_nested_expectations_and_message_transcript_shape() -> None:
    case = EvalCase.from_dict(
        {
            "id": "nested",
            "scenario": "nested",
            "expected": {
                "status": "qualified",
                "reason_codes": ["all_explicit_criteria_met"],
                "canonical_fields": {"full_name": "Ada"},
                "language": "en",
                "language_trace": ["es", "en"],
                "faq_answered": True,
                "faq_contains": "schedule",
                "correction": {"field": "full_name", "confirmed": True},
                "error_type": "RuntimeError",
            },
        }
    )
    assert case.expected_status is ScreeningStatus.QUALIFIED
    assert case.expected_reason_codes == ("all_explicit_criteria_met",)
    assert case.expected_fields == {"full_name": "Ada"}
    assert case.expected_language is Language.EN
    assert case.expected_language_trace == (Language.ES, Language.EN)
    assert case.expected_faq_answered is True
    assert case.expected_faq_contains == ("schedule",)
    assert case.expected_correction_field == "full_name"
    assert case.expected_correction_confirmed is True
    assert case.expected_error == "RuntimeError"

    scenario = EvalScenario.from_dict(
        {
            "id": "messages",
            "language": "en",
            "messages": [
                {"role": "assistant", "content": "ignored"},
                {"role": "candidate", "content": "kept"},
                {"role": "user", "content": "also kept"},
                {"role": "tool", "content": "ignored"},
            ],
            "expected": {},
        }
    )
    assert scenario.turns == ("kept", "also kept")


def test_eval_fragment_matching_is_partial_nested_and_rejects_wrong_values() -> None:
    actual = {"location": {"service_area_id": "es-mad-centro", "confirmed": True}}
    assert _value_matches(actual["location"], {"confirmed": True})
    assert _fields_match(actual, {"location.service_area_id": "es-mad-centro"})
    assert not _fields_match(actual, {"location.confirmed": False})


def test_missing_scenario_is_reported_as_failed_case() -> None:
    report = run_deterministic([EvalCase("missing", "unknown", ScreeningStatus.IN_PROGRESS)], {})
    assert report.failed == 1
    assert report.results[0].error == "scenario_not_found"


def test_loaders_reject_invalid_shapes(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"scenarios": "not-a-list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="scenario data"):
        load_scenarios(invalid)
    invalid.write_text(json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="eval data"):
        load_eval_cases(invalid)


class _ReplayInterpreter:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.messages: list[str] = []

    async def interpret(
        self,
        message: str,
        dependencies: InterpreterDependencies,
        *,
        message_history: Sequence[Any] = (),
    ) -> InterpreterResult:
        _ = (message_history, dependencies)
        self.messages.append(message)
        if message == self.fail_on:
            raise RuntimeError("simulated provider failure")
        state = dependencies.state
        return InterpreterResult(_deterministic_interpretation(message, state))


@pytest.mark.asyncio
async def test_live_runner_uses_injected_interpreter_and_reports_retryable_failure() -> None:
    scenario = EvalScenario(
        id="live",
        language=Language.EN,
        turns=("Ada Lovelace", "__provider_failure__"),
        expected={
            "status": "in_progress",
            "reason_codes": ["required_information_missing"],
            "retryable": True,
            "error_type": "RuntimeError",
            "language": "en",
            "canonical_fields": {"full_name": "Ada Lovelace"},
        },
    )
    case = EvalCase("live", "live", ScreeningStatus.IN_PROGRESS, max_turns=2, retryable=True)
    interpreter = _ReplayInterpreter(fail_on="__provider_failure__")

    report = await run_live([case], {"live": scenario}, interpreter=interpreter)

    assert report.failed == 0
    assert interpreter.messages == ["Ada Lovelace", "__provider_failure__"]
    assert report.results[0].retryable is True
    assert report.results[0].canonical_fields["full_name"] == "Ada Lovelace"


def test_check_result_rejects_status_only_success_when_reason_or_state_is_wrong() -> None:
    scenario = EvalScenario(
        id="strict",
        language=Language.EN,
        turns=(),
        expected={
            "reason_codes": ["all_explicit_criteria_met"],
            "canonical_fields": {"full_name": "Ada"},
            "language": "en",
        },
    )
    result = _check_result(
        EvalCase("strict", "strict", ScreeningStatus.QUALIFIED),
        scenario,
        status=ScreeningStatus.QUALIFIED,
        turns=0,
        security_event=False,
        retryable=False,
        reason_codes=["required_information_missing"],
        state=ScreeningState.empty(Language.EN),
    )
    assert result.passed is False
