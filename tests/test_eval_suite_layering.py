import json
import subprocess
import sys
from pathlib import Path

from scripts.run_evals import evaluate_case, filter_cases_by_suite, load_cases, run_evals


EXPECTED_SUITES = {
    "routing",
    "e2e",
    "provider_safety",
    "memory",
    "memory_quality",
    "packaging",
    "plan_mode",
    "durable_tasks",
}


def test_eval_cases_have_suite_and_category_fields() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))

    assert all(case["suite"] in EXPECTED_SUITES for case in cases)
    assert all(case["category"] for case in cases)
    for suite in EXPECTED_SUITES:
        assert any(case["suite"] == suite for case in cases)


def test_eval_runner_returns_suite_level_summary() -> None:
    summary = run_evals(load_cases(Path("tests/evals/eval_cases.json")))

    assert "suites" in summary
    assert EXPECTED_SUITES.issubset(set(summary["suites"]))
    assert summary["suites"]["routing"]["total"] > 0
    assert summary["suites"]["e2e"]["total"] > 0
    assert summary["total"] == sum(suite["total"] for suite in summary["suites"].values())


def test_eval_cases_can_be_filtered_by_suite() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    routing_cases = filter_cases_by_suite(cases, "routing")
    e2e_cases = filter_cases_by_suite(cases, "e2e")
    provider_safety_cases = filter_cases_by_suite(cases, "provider_safety")
    memory_cases = filter_cases_by_suite(cases, "memory")
    memory_quality_cases = filter_cases_by_suite(cases, "memory_quality")
    packaging_cases = filter_cases_by_suite(cases, "packaging")
    plan_mode_cases = filter_cases_by_suite(cases, "plan_mode")
    durable_task_cases = filter_cases_by_suite(cases, "durable_tasks")

    assert routing_cases
    assert e2e_cases
    assert provider_safety_cases
    assert memory_cases
    assert memory_quality_cases
    assert packaging_cases
    assert plan_mode_cases
    assert durable_task_cases
    assert all(case["suite"] == "routing" for case in routing_cases)
    assert all(case["suite"] == "e2e" for case in e2e_cases)
    assert all(case["suite"] == "provider_safety" for case in provider_safety_cases)
    assert all(case["suite"] == "memory" for case in memory_cases)
    assert all(case["suite"] == "memory_quality" for case in memory_quality_cases)
    assert all(case["suite"] == "packaging" for case in packaging_cases)
    assert all(case["suite"] == "plan_mode" for case in plan_mode_cases)
    assert all(case["suite"] == "durable_tasks" for case in durable_task_cases)


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


def test_plan_mode_eval_suite_runs_scripted_native_plan_mode_offline() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "plan_mode")

    summary = run_evals(cases)

    assert summary["total"] >= 4
    assert summary["failed"] == 0
    assert set(summary["suites"]) == {"plan_mode"}
    assert summary["suites"]["plan_mode"]["passed"] == summary["total"]


def test_plan_mode_eval_detail_exposes_native_runtime_checks() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "plan_mode")
    case = next(item for item in cases if item["id"] == "plan_mode_revise_after_tool_failure_001")

    detail = evaluate_case(case)

    assert detail["passed"] is True
    assert detail["execution_strategy"] == "plan_and_solve"
    assert detail["plan_revision_count"] == 0
    assert detail["plan_mode_checks"]["api_contract_match"] is True
    assert detail["plan_mode_checks"]["native_runtime_match"] is True
    assert detail["plan_mode_checks"]["plan_revision_match"] is True
    assert detail["actual_tools"] == ["price_compare", "image_generation"]
    assert detail["error_codes"] == []


def test_durable_task_eval_suite_runs_offline_native_control_flow() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "durable_tasks")

    summary = run_evals(cases)

    assert summary["total"] == 8
    assert summary["failed"] == 0
    assert summary["suites"]["durable_tasks"]["passed"] == 8


def test_durable_task_eval_detail_reports_governance_and_redaction() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "durable_tasks")
    case = next(item for item in cases if item["id"] == "durable_waiting_confirmation_001")

    detail = evaluate_case(case)

    assert detail["passed"] is True
    assert detail["task_status"] == "waiting_confirmation"
    assert detail["plan_version"] == 1
    assert detail["chat_calls"] == 1
    assert detail["actual_tools"] == ["custom_notification"]
    assert detail["raw_payload_safe"] is True
    assert detail["durable_task_checks"]["tool_governance_evidence"] is True
