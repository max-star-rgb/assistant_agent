from pathlib import Path

from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


def test_memory_eval_suite_contains_required_cases() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "memory")
    case_ids = {case["id"] for case in cases}

    assert {
        "memory_preference_to_image_generation_001",
        "memory_product_to_render_001",
        "memory_task_resume_001",
        "memory_user_isolation_001",
        "memory_delete_user_scoped_001",
    }.issubset(case_ids)


def test_memory_eval_suite_passes_offline() -> None:
    cases = filter_cases_by_suite(load_cases(Path("tests/evals/eval_cases.json")), "memory")

    summary = run_evals(cases)

    assert summary["failed"] == 0
    assert summary["passed"] == len(cases)
    assert summary["suites"]["memory"]["failed"] == 0
