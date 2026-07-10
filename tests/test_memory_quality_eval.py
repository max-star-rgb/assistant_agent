import json
import subprocess
import sys
from pathlib import Path

from assistant_agent.memory.quality_eval import (
    evaluate_memory_quality_case,
    summarize_memory_quality_eval,
)
from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


def test_memory_quality_eval_classifies_write_reject_and_confirmation_cases() -> None:
    write_result = evaluate_memory_quality_case(
        {
            "id": "explicit_preference_write",
            "case_kind": "explicit",
            "text": "记住我喜欢短回答",
            "content": {"summary": "用户喜欢短回答。", "preference_key": "answer_length"},
            "expected_action": "write",
            "expected_destination": "user_profile",
            "expected_sensitivity": "low",
        }
    )
    reject_result = evaluate_memory_quality_case(
        {
            "id": "explicit_secret_reject",
            "case_kind": "explicit",
            "text": "记住我的服务配置",
            "content": {"api_key": "placeholder"},
            "expected_action": "reject",
            "expected_destination": "reject",
            "expected_sensitivity": "secret",
        }
    )
    confirmation_result = evaluate_memory_quality_case(
        {
            "id": "explicit_sensitive_confirmation",
            "case_kind": "explicit",
            "text": "记住我的项目路径是 /home/alice/private/project",
            "expected_action": "confirm",
            "expected_destination": "task_checkpoint",
            "expected_sensitivity": "high",
        }
    )

    assert write_result.passed is True
    assert write_result.actual_action == "write"
    assert write_result.feedback == {
        "action": "write",
        "allowed": True,
        "destination": "user_profile",
        "requires_confirmation": False,
        "sensitivity": "low",
    }
    assert reject_result.passed is True
    assert reject_result.actual_action == "reject"
    assert "placeholder" not in str(reject_result.model_dump(mode="json")).lower()
    assert confirmation_result.passed is True
    assert confirmation_result.actual_action == "confirm"
    assert confirmation_result.feedback["requires_confirmation"] is True
    assert "/home/alice" not in str(confirmation_result.model_dump(mode="json"))


def test_memory_quality_eval_summarizes_policy_feedback_metrics() -> None:
    results = [
        evaluate_memory_quality_case(
            {
                "id": "write",
                "case_kind": "explicit",
                "text": "记住我喜欢短回答",
                "content": {"summary": "用户喜欢短回答。"},
                "expected_action": "write",
            }
        ),
        evaluate_memory_quality_case(
            {
                "id": "reject",
                "case_kind": "explicit",
                "text": "记住我的服务配置",
                "content": {"api_key": "placeholder"},
                "expected_action": "reject",
                "expected_sensitivity": "secret",
            }
        ),
        evaluate_memory_quality_case(
            {
                "id": "confirm",
                "case_kind": "explicit",
                "text": "记住我的项目路径是 /home/alice/private/project",
                "expected_action": "confirm",
            }
        ),
    ]

    summary = summarize_memory_quality_eval(results)

    assert summary["total"] == 3
    assert summary["passed"] == 3
    assert summary["action_accuracy"] == 1.0
    assert summary["write_precision"] == 1.0
    assert summary["reject_recall"] == 1.0
    assert summary["confirmation_recall"] == 1.0
    assert summary["secret_rejection_rate"] == 1.0
    assert summary["false_write_rate"] == 0.0


def test_run_evals_memory_quality_suite_includes_quality_metrics() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "memory_quality")

    summary = run_evals(cases)

    assert summary["failed"] == 0
    assert summary["total"] >= 4
    assert set(summary["suites"]) == {"memory_quality"}
    quality_summary = summary["memory_quality_eval"]
    assert quality_summary["total"] == summary["total"]
    assert quality_summary["action_accuracy"] == 1.0
    assert quality_summary["false_write_rate"] == 0.0


def test_run_evals_cli_supports_memory_quality_suite() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_evals.py", "--suite", "memory_quality"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(result.stdout)

    assert summary["selected_suite"] == "memory_quality"
    assert summary["failed"] == 0
    assert set(summary["suites"]) == {"memory_quality"}
    assert summary["memory_quality_eval"]["action_accuracy"] == 1.0
