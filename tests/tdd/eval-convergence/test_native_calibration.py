from types import SimpleNamespace

import pytest

from evals.agent import calibration
from evals.agent.langfuse_backend import _calibration_case_key
from evals.agent.loader import load_task


def test_native_calibration_cases_use_human_labels_for_canonical_scores() -> None:
    task = load_task("deep_research_autonomous_admission")

    cases = calibration.build_native_calibration_cases([task])

    assert len(cases) >= 2
    assert all(case.task.id == task.id for case in cases)
    assert all(
        set(case.expected_scores)
        == {"task_conformance", "grounding", "response_quality"}
        for case in cases
    )
    assert all(case.evidence.task_id == task.id for case in cases)


def test_native_calibration_compares_persisted_langfuse_scores() -> None:
    task = load_task("deep_research_autonomous_admission")
    cases = calibration.build_native_calibration_cases([task])
    persisted_scores = [dict(case.expected_scores) for case in cases]

    results = calibration.compare_native_calibration_scores(cases, persisted_scores)

    assert len(results) == len(cases)
    assert all(result.matched for result in results)
    assert all(
        result.actual_scores == result.expected_scores
        for result in results
    )


def test_native_calibration_rejects_unstructured_dataset_metadata() -> None:
    with pytest.raises(RuntimeError, match="metadata must be an object"):
        _calibration_case_key(SimpleNamespace(metadata=None))
