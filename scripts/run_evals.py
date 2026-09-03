#!/usr/bin/env python3
"""Run deterministic or live conversation evaluations.

The deterministic mode is the default and never needs an API key.  Live mode
is explicit because it can incur provider cost and latency::

    python scripts/run_evals.py --mode deterministic
    python scripts/run_evals.py --mode live --json

Live provider smoke runs can be bounded to selected fixtures with repeated
``--case-id`` options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from candidate_screening.config import Settings  # noqa: E402
from candidate_screening.evals import (  # noqa: E402
    DEFAULT_EVALS_PATH,
    DEFAULT_SCENARIOS_PATH,
    load_eval_cases,
    load_scenarios,
    run_deterministic,
    run_live,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run candidate-screening conversation evaluations")
    parser.add_argument("--mode", choices=("deterministic", "live"), default="deterministic")
    parser.add_argument(
        "--evals", type=Path, default=DEFAULT_EVALS_PATH, help="evaluation case JSON"
    )
    parser.add_argument(
        "--scenarios", type=Path, default=DEFAULT_SCENARIOS_PATH, help="scenario JSON"
    )
    parser.add_argument(
        "--case-id",
        dest="case_ids",
        action="append",
        help="run only this evaluation case (repeat for multiple cases)",
    )
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument(
        "--fail-on-error", action="store_true", help="exit 1 when one or more cases fail"
    )
    return parser


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    cases = load_eval_cases(args.evals)
    scenarios = load_scenarios(args.scenarios)
    if args.case_ids:
        requested = set(args.case_ids)
        known = {case.id for case in cases}
        missing = sorted(requested - known)
        if missing:
            raise ValueError(f"unknown evaluation case(s): {', '.join(missing)}")
        cases = tuple(case for case in cases if case.id in requested)
    if args.mode == "live":
        report = await run_live(cases, scenarios, settings=Settings())
    else:
        report = run_deterministic(cases, scenarios)
    return report.as_dict(), report.failed > 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload, failed = asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"mode={payload['mode']} passed={payload['passed']}/{payload['total']} failed={payload['failed']}"
        )
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("evaluation report contains an invalid results value")
        result_items = cast(list[object], results)
        for result in result_items:
            if isinstance(result, dict):
                result_payload = cast(dict[str, Any], result)
            else:
                continue
            if not result_payload.get("passed", False):
                print(
                    f"FAIL {result_payload.get('id')}: "
                    f"{result_payload.get('error') or result_payload.get('actual_status')}"
                )
    return 1 if args.fail_on_error and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
