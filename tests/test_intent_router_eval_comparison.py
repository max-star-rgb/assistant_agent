import json
import subprocess
import sys
from pathlib import Path

from scripts.run_evals import load_cases, run_evals


def _comparison_cases() -> list[dict[str, object]]:
    return [
        case
        for case in load_cases(Path("tests/evals/eval_cases.json"))
        if case.get("category") == "router_comparison"
    ]


def test_router_comparison_cases_are_present() -> None:
    cases = _comparison_cases()

    assert len(cases) >= 5
    assert all("router_expectations" in case for case in cases)


def test_rule_router_eval_summary_contains_router_mode() -> None:
    summary = run_evals(_comparison_cases(), router_mode="rule")

    assert summary["router_mode"] == "rule"
    assert "rule" in summary["routers"]
    assert summary["failed_case_ids"] == []


def test_mock_llm_router_eval_runs_offline() -> None:
    summary = run_evals(_comparison_cases(), router_mode="mock_llm")

    assert summary["router_mode"] == "mock_llm"
    assert "mock_llm" in summary["routers"]
    assert summary["total"] == len(_comparison_cases())
    assert "failed_case_ids" in summary


def test_hybrid_router_eval_runs_offline() -> None:
    summary = run_evals(_comparison_cases(), router_mode="hybrid")

    assert summary["router_mode"] == "hybrid"
    assert "hybrid" in summary["routers"]
    assert summary["failed_case_ids"] == []


def test_run_evals_cli_accepts_router_argument_without_real_llm() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_evals.py", "--suite", "routing", "--router", "hybrid"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)

    assert summary["selected_suite"] == "routing"
    assert summary["router_mode"] == "hybrid"
    assert "hybrid" in summary["routers"]
