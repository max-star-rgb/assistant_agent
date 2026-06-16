from pathlib import Path

from scripts.run_evals import load_cases, run_evals


VIDEO_CASE_IDS = {
    "video_understanding_001",
    "media_video_search_001",
    "video_to_price_compare_001",
    "video_to_image_generation_001",
    "video_understanding_to_render",
    "video_to_memory_save_001",
    "video_missing_input_followup_001",
    "video_present_but_text_chat_001",
}


def test_video_eval_cases_are_present() -> None:
    cases = load_cases(Path("tests/evals/eval_cases.json"))
    case_ids = {case["id"] for case in cases}

    assert VIDEO_CASE_IDS.issubset(case_ids)


def test_video_eval_cases_pass_offline_with_default_adapters() -> None:
    cases = [case for case in load_cases(Path("tests/evals/eval_cases.json")) if case["id"] in VIDEO_CASE_IDS]

    summary = run_evals(cases)

    assert summary["total"] == len(VIDEO_CASE_IDS)
    assert summary["passed"] == len(VIDEO_CASE_IDS)
    assert summary["failed"] == 0
