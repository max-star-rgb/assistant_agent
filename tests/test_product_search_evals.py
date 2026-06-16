from pathlib import Path

from scripts.run_evals import load_cases, run_evals


PHASE5C_CATEGORIES = {
    "text_only_product_search",
    "text_only_price_compare",
    "media_plus_search",
    "media_plus_search_compare",
    "product_search_price_compare",
}


def test_phase5c_eval_cases_cover_product_search_and_price_compare() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    by_category = {category: [case for case in cases if case["category"] == category] for category in PHASE5C_CATEGORIES}

    assert len(by_category["text_only_product_search"]) >= 3
    assert len(by_category["text_only_price_compare"]) >= 3
    assert len(by_category["media_plus_search"]) >= 2
    assert len(by_category["media_plus_search_compare"]) >= 1
    assert len(by_category["product_search_price_compare"]) >= 2
    assert all(
        case["expected_tools"] == ["product_search"]
        for case in by_category["text_only_product_search"]
    )
    assert all(
        "price_compare" in case["expected_tools"]
        for case in by_category["text_only_price_compare"] + by_category["product_search_price_compare"]
    )


def test_phase5c_eval_subset_passes_offline_with_default_adapters() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    subset = [case for case in cases if case["category"] in PHASE5C_CATEGORIES]

    summary = run_evals(subset)

    assert summary["failed"] == 0
    assert summary["passed"] == summary["total"]
    assert summary["tool_selection_accuracy"] == 1.0
    assert summary["ordered_tool_match"] == 1.0
