import json
import subprocess
import sys
from pathlib import Path

from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


def test_provider_safety_eval_suite_exists_and_passes_offline() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "provider_safety")

    summary = run_evals(cases)

    assert len(cases) >= 5
    assert {case["safety_scenario"] for case in cases} == {
        "provider_timeout",
        "provider_bad_response",
        "provider_unconfigured",
        "provider_budget_exceeded",
        "provider_rate_limited",
    }
    assert summary["total"] == len(cases)
    assert summary["failed"] == 0
    assert summary["suites"]["provider_safety"]["failed"] == 0


def test_provider_safety_eval_cli_runs_selected_suite_offline() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_evals.py", "--suite", "provider_safety"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)

    assert summary["selected_suite"] == "provider_safety"
    assert summary["total"] >= 5
    assert set(summary["suites"]) == {"provider_safety"}
    assert summary["failed"] == 0
