from collections import Counter
from pathlib import Path

from scripts.run_evals import load_cases, run_evals


REQUIRED_SUMMARY_KEYS = {
    "total",
    "passed",
    "failed",
    "suites",
    "routers",
    "router_mode",
    "pass_rate",
    "intent_accuracy",
    "capability_accuracy",
    "tool_selection_accuracy",
    "ordered_tool_match",
    "unexpected_tool_rate",
    "media_requirement_error_rate",
    "followup_accuracy",
    "response_quality_pass_rate",
    "failed_case_ids",
}

REQUIRED_ROUTING_CATEGORIES = {
    "direct_chat",
    "text_only_image_generation",
    "text_only_product_search",
    "text_only_price_compare",
    "text_only_render",
    "image_understanding",
    "video_understanding",
    "media_plus_generation",
    "media_plus_search_compare",
    "memory_retrieval",
    "multi_step_orchestration",
    "ambiguous_followup",
}


def test_eval_cases_are_parseable_and_cover_required_count() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    category_counts = Counter(case["category"] for case in cases)

    assert len(cases) >= 40
    assert category_counts["intent"] >= 5
    assert category_counts["routing"] >= 5
    assert category_counts["multistep"] >= 8
    assert category_counts["memory"] >= 4
    assert category_counts["failure"] >= 4
    assert category_counts["multimodal"] >= 4
    assert REQUIRED_ROUTING_CATEGORIES.issubset(set(category_counts))
    assert all("id" in case for case in cases)
    assert all("suite" in case for case in cases)
    assert all("category" in case for case in cases)
    assert all("expected_tools" in case for case in cases)
    assert all("expected_intent" in case for case in cases)


def test_eval_runner_returns_expanded_summary_shape() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))

    summary = run_evals(cases[:2])

    assert set(summary) == REQUIRED_SUMMARY_KEYS
    assert summary["total"] == 2
    assert summary["passed"] + summary["failed"] == 2
    assert 0.0 <= summary["intent_accuracy"] <= 1.0
    assert 0.0 <= summary["capability_accuracy"] <= 1.0
    assert 0.0 <= summary["tool_selection_accuracy"] <= 1.0
    assert 0.0 <= summary["ordered_tool_match"] <= 1.0
    assert 0.0 <= summary["unexpected_tool_rate"] <= 1.0
    assert 0.0 <= summary["media_requirement_error_rate"] <= 1.0
    assert 0.0 <= summary["followup_accuracy"] <= 1.0
    assert 0.0 <= summary["response_quality_pass_rate"] <= 1.0
