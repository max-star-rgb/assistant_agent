import json
import subprocess
import sys
from pathlib import Path

from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


def test_eval_cases_have_suite_and_category_fields() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))

    assert all(case["suite"] in {"routing", "e2e", "provider_safety"} for case in cases)
    assert all(case["category"] for case in cases)
    assert any(case["suite"] == "routing" for case in cases)
    assert any(case["suite"] == "e2e" for case in cases)
    assert any(case["suite"] == "provider_safety" for case in cases)


def test_eval_runner_returns_suite_level_summary() -> None:
    summary = run_evals(load_cases(Path("tests/evals/eval_cases.json")))

    assert "suites" in summary
    assert {"routing", "e2e", "provider_safety"}.issubset(set(summary["suites"]))
    assert summary["suites"]["routing"]["total"] > 0
    assert summary["suites"]["e2e"]["total"] > 0
    assert summary["total"] == (
        summary["suites"]["routing"]["total"] + summary["suites"]["e2e"]["total"]
        + summary["suites"]["provider_safety"]["total"]
    )


def test_eval_cases_can_be_filtered_by_suite() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    routing_cases = filter_cases_by_suite(cases, "routing")
    e2e_cases = filter_cases_by_suite(cases, "e2e")
    provider_safety_cases = filter_cases_by_suite(cases, "provider_safety")

    assert routing_cases
    assert e2e_cases
    assert provider_safety_cases
    assert all(case["suite"] == "routing" for case in routing_cases)
    assert all(case["suite"] == "e2e" for case in e2e_cases)
    assert all(case["suite"] == "provider_safety" for case in provider_safety_cases)


def test_e2e_eval_cases_reference_demo_scenario_matrix() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    scenarios = json.loads(
        Path("demo_data/scenarios/e2e_demo_scenarios.json").read_text(encoding="utf-8")
    )
    scenario_ids = {scenario["scenario_id"] for scenario in scenarios}
    referenced_ids = {
        case["scenario_id"]
        for case in cases
        if case["suite"] == "e2e" and case.get("scenario_id")
    }

    assert referenced_ids
    assert referenced_ids.issubset(scenario_ids)


def test_failed_case_ids_are_preserved_for_suite_summary() -> None:
    case = dict(load_cases(Path("tests/evals/eval_cases.json"))[0])
    case["id"] = "forced_failure"
    case["expected_tools"] = ["nonexistent_tool"]

    summary = run_evals([case])

    assert summary["failed"] == 1
    assert summary["failed_case_ids"] == ["forced_failure"]
    assert summary["suites"][case["suite"]]["failed_case_ids"] == ["forced_failure"]


def test_run_evals_cli_supports_suite_filter_offline() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_evals.py", "--suite", "routing"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)

    assert summary["selected_suite"] == "routing"
    assert summary["total"] > 0
    assert set(summary["suites"]) == {"routing"}
    assert summary["failed"] == 0
