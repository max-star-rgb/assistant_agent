"""Direct grader calibration against labeled positive and negative evidence."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from evals.agent.contracts import (
    GraderResult,
    RunEvidence,
    SemanticJudge,
    SemanticVerdict,
    TaskSpec,
)
from evals.agent.loader import TASKS_ROOT, load_entrypoint


class CalibrationFixture(BaseModel):
    id: str = Field(min_length=1)
    expected_pass: bool
    semantic_verdict: SemanticVerdict
    evidence: dict[str, Any]


class CalibrationSet(BaseModel):
    schema_version: str
    fixtures: list[CalibrationFixture] = Field(min_length=2)


class CalibrationResult(BaseModel):
    fixture_id: str
    expected_pass: bool
    actual_pass: bool
    expected_semantic_pass: bool
    actual_semantic_pass: bool
    matched: bool
    reason: str


def run_calibration(
    task: TaskSpec,
    judge: SemanticJudge,
) -> list[CalibrationResult]:
    payload = CalibrationSet.model_validate_json(
        (TASKS_ROOT / task.id / "calibration.json").read_text(encoding="utf-8")
    )
    grader = load_entrypoint(task.grader)
    results: list[CalibrationResult] = []
    for fixture in payload.fixtures:
        evidence_payload = _replace_tomorrow(fixture.evidence)
        evidence = RunEvidence.model_validate(evidence_payload)
        recording_judge = _RecordingJudge(judge)
        graded: GraderResult = grader(evidence, recording_judge)
        semantic = recording_judge.last_verdict
        if semantic is None:
            raise RuntimeError(
                f"Calibration fixture {fixture.id!r} did not invoke the judge."
            )
        matched = (
            graded.passed == fixture.expected_pass
            and semantic.passed == fixture.semantic_verdict.passed
        )
        results.append(
            CalibrationResult(
                fixture_id=fixture.id,
                expected_pass=fixture.expected_pass,
                actual_pass=graded.passed,
                expected_semantic_pass=fixture.semantic_verdict.passed,
                actual_semantic_pass=semantic.passed,
                matched=matched,
                reason=graded.reason,
            )
        )
    return results


class LabeledCalibrationJudge:
    """Offline test judge that replays the human label for each fixture."""

    def __init__(self, verdicts: list[SemanticVerdict]) -> None:
        self._verdicts = iter(verdicts)

    def evaluate(
        self,
        *,
        criterion: str,
        evidence: RunEvidence,
    ) -> SemanticVerdict:
        del criterion, evidence
        return next(self._verdicts)


def load_labeled_calibration_judge(task: TaskSpec) -> LabeledCalibrationJudge:
    payload = CalibrationSet.model_validate_json(
        (TASKS_ROOT / task.id / "calibration.json").read_text(encoding="utf-8")
    )
    return LabeledCalibrationJudge(
        [fixture.semantic_verdict for fixture in payload.fixtures]
    )


class _RecordingJudge:
    def __init__(self, delegate: SemanticJudge) -> None:
        self.delegate = delegate
        self.last_verdict: SemanticVerdict | None = None

    def evaluate(
        self,
        *,
        criterion: str,
        evidence: RunEvidence,
    ) -> SemanticVerdict:
        self.last_verdict = self.delegate.evaluate(
            criterion=criterion,
            evidence=evidence,
        )
        return self.last_verdict


def _replace_tomorrow(value: Any) -> Any:
    if value == "__TOMORROW__":
        return (date.today() + timedelta(days=1)).isoformat()
    if isinstance(value, list):
        return [_replace_tomorrow(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tomorrow(item) for key, item in value.items()}
    return value
