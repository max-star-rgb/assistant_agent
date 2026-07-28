"""Direct grader calibration against labeled positive and negative evidence."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from evals.agent.contracts import (
    GraderResult,
    JudgeVerdict,
    LLMJudge,
    RunEvidence,
    TaskSpec,
)
from evals.agent.grading import DIMENSION_NAMES, grade_task
from evals.agent.loader import TASKS_ROOT


class CalibrationFixture(BaseModel):
    id: str = Field(min_length=1)
    expected_pass: bool
    judge_verdicts: dict[str, JudgeVerdict] = Field(min_length=1)
    evidence: dict[str, Any]


class CalibrationSet(BaseModel):
    schema_version: Literal["agent_eval_calibration_v2"]
    fixtures: list[CalibrationFixture] = Field(min_length=2)


class CalibrationResult(BaseModel):
    fixture_id: str
    expected_pass: bool
    actual_pass: bool
    expected_judge_passes: dict[str, bool]
    actual_judge_passes: dict[str, bool]
    dimensions: dict[str, bool]
    matched: bool
    reason: str


def run_calibration(
    task: TaskSpec,
    judge: LLMJudge,
) -> list[CalibrationResult]:
    payload = CalibrationSet.model_validate_json(
        (TASKS_ROOT / task.id / "calibration.json").read_text(encoding="utf-8")
    )
    results: list[CalibrationResult] = []
    for fixture in payload.fixtures:
        evidence_payload = _replace_tomorrow(fixture.evidence)
        evidence = RunEvidence.model_validate(evidence_payload)
        recording_judge = _RecordingJudge(judge)
        graded: GraderResult = grade_task(
            task=task,
            evidence=evidence,
            judge=recording_judge,
        )
        if not recording_judge.verdicts:
            raise RuntimeError(
                f"Calibration fixture {fixture.id!r} did not invoke the judge."
            )
        expected_judge_passes = {
            criterion_id: verdict.passed
            for criterion_id, verdict in fixture.judge_verdicts.items()
        }
        actual_judge_passes = {
            criterion_id: verdict.passed
            for criterion_id, verdict in recording_judge.verdicts.items()
        }
        matched = (
            graded.passed == fixture.expected_pass
            and actual_judge_passes == expected_judge_passes
        )
        results.append(
            CalibrationResult(
                fixture_id=fixture.id,
                expected_pass=fixture.expected_pass,
                actual_pass=graded.passed,
                expected_judge_passes=expected_judge_passes,
                actual_judge_passes=actual_judge_passes,
                dimensions={
                    name: getattr(graded.dimensions, name).passed
                    for name in DIMENSION_NAMES
                },
                matched=matched,
                reason=graded.reason,
            )
        )
    return results


class LabeledCalibrationJudge:
    """Offline test judge that replays the human label for each fixture."""

    def __init__(self, verdicts: list[tuple[str, JudgeVerdict]]) -> None:
        self._verdicts = iter(verdicts)

    def evaluate(
        self,
        *,
        criterion_id: str,
        rubric: str,
        evidence: RunEvidence,
    ) -> JudgeVerdict:
        del rubric, evidence
        expected_criterion_id, verdict = next(self._verdicts)
        if criterion_id != expected_criterion_id:
            raise RuntimeError(
                "Calibration judge criterion order mismatch: "
                f"expected={expected_criterion_id!r}, actual={criterion_id!r}."
            )
        return verdict


def load_labeled_calibration_judge(task: TaskSpec) -> LabeledCalibrationJudge:
    payload = CalibrationSet.model_validate_json(
        (TASKS_ROOT / task.id / "calibration.json").read_text(encoding="utf-8")
    )
    return LabeledCalibrationJudge(
        [
            (criterion_id, verdict)
            for fixture in payload.fixtures
            for criterion_id, verdict in fixture.judge_verdicts.items()
        ]
    )


class _RecordingJudge:
    def __init__(self, delegate: LLMJudge) -> None:
        self.delegate = delegate
        self.verdicts: dict[str, JudgeVerdict] = {}

    def evaluate(
        self,
        *,
        criterion_id: str,
        rubric: str,
        evidence: RunEvidence,
    ) -> JudgeVerdict:
        if criterion_id in self.verdicts:
            raise RuntimeError(
                f"Grader invoked duplicate judge criterion {criterion_id!r}."
            )
        verdict = self.delegate.evaluate(
            criterion_id=criterion_id,
            rubric=rubric,
            evidence=evidence,
        )
        self.verdicts[criterion_id] = verdict
        return verdict


def _replace_tomorrow(value: Any) -> Any:
    if value == "__TOMORROW__":
        return (date.today() + timedelta(days=1)).isoformat()
    if isinstance(value, list):
        return [_replace_tomorrow(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tomorrow(item) for key, item in value.items()}
    return value
