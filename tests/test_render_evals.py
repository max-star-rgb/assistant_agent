from scripts.run_evals import load_cases, run_evals


RENDER_CASE_IDS = {
    "text_only_render",
    "product_search_to_render",
    "image_understanding_to_render",
    "video_understanding_to_render",
    "memory_to_render",
}


def test_render_eval_cases_are_present() -> None:
    cases = load_cases()
    case_ids = {case["id"] for case in cases}

    assert RENDER_CASE_IDS.issubset(case_ids)


def test_render_eval_cases_pass_offline() -> None:
    cases = [case for case in load_cases() if case["id"] in RENDER_CASE_IDS]

    summary = run_evals(cases)

    assert summary["total"] == 5
    assert summary["passed"] == 5
    assert summary["failed"] == 0
