from pathlib import Path

from scripts.run_evals import load_cases, run_evals


def test_text_capability_eval_cases_cover_direct_chat_and_image_generation() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    direct_chat_cases = [case for case in cases if case["category"] == "direct_chat"]
    image_generation_cases = [
        case for case in cases if case["category"] == "text_only_image_generation"
    ]

    assert len(direct_chat_cases) >= 4
    assert len(image_generation_cases) >= 4
    assert all(case["expected_tools"] == [] for case in direct_chat_cases)
    assert all(case["expected_tools"] == ["image_generation"] for case in image_generation_cases)
    assert all(
        "vision_understanding" in case.get("must_not_call", []) for case in direct_chat_cases
    )
    assert all(
        {"image", "video"}.issubset(set(case.get("must_not_require", [])))
        for case in image_generation_cases
    )


def test_text_capability_eval_subset_passes_offline_with_default_adapters() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    subset = [
        case
        for case in cases
        if case["category"] in {"direct_chat", "text_only_image_generation"}
    ]

    summary = run_evals(subset)

    assert summary["failed"] == 0
    assert summary["passed"] == summary["total"]
    assert summary["intent_accuracy"] == 1.0
    assert summary["tool_selection_accuracy"] == 1.0
