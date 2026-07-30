"""Direct grader calibration against labeled positive and negative evidence."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evals.agent.contracts import (
    GraderResult,
    JudgeVerdict,
    LLMJudge,
    RunEvidence,
    TaskSpec,
)
from evals.agent.grading import DIMENSION_NAMES, grade_task
from evals.agent.loader import calibration_path


class CalibrationDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_execution: bool
    tool_semantics: bool
    grounding: bool
    response_quality: bool


class CalibrationJudgeVerdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_semantics: JudgeVerdict
    grounding: JudgeVerdict
    response_quality: JudgeVerdict


class CalibrationFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    expected_dimensions: CalibrationDimensions
    judge_verdicts: CalibrationJudgeVerdicts
    evidence: dict[str, Any]


class CalibrationSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent_eval_calibration_v3"]
    fixtures: list[CalibrationFixture] = Field(min_length=2)


class CalibrationResult(BaseModel):
    fixture_id: str
    expected_dimensions: dict[str, bool]
    expected_judge_passes: dict[str, bool]
    actual_judge_passes: dict[str, bool]
    dimensions: dict[str, bool]
    matched: bool
    reason: str


def _calibration_judge_verdicts(
    verdicts: CalibrationJudgeVerdicts,
) -> list[tuple[str, JudgeVerdict]]:
    return [
        (criterion_id, getattr(verdicts, criterion_id))
        for criterion_id in ("tool_semantics", "grounding", "response_quality")
    ]


def run_calibration(
    task: TaskSpec,
    judge: LLMJudge,
) -> list[CalibrationResult]:
    payload = CalibrationSet.model_validate_json(
        calibration_path(task.id).read_text(encoding="utf-8")
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
            for criterion_id, verdict in _calibration_judge_verdicts(
                fixture.judge_verdicts
            )
        }
        actual_judge_passes = {
            criterion_id: verdict.passed
            for criterion_id, verdict in recording_judge.verdicts.items()
        }
        dimensions = {
            name: getattr(graded.dimensions, name).passed
            for name in DIMENSION_NAMES
        }
        matched = (
            dimensions == fixture.expected_dimensions.model_dump()
            and actual_judge_passes == expected_judge_passes
        )
        results.append(
            CalibrationResult(
                fixture_id=fixture.id,
                expected_dimensions=fixture.expected_dimensions.model_dump(),
                expected_judge_passes=expected_judge_passes,
                actual_judge_passes=actual_judge_passes,
                dimensions=dimensions,
                matched=matched,
                reason=(
                    "四个独立 Score 与人工标注一致。"
                    if matched
                    else "Score 与人工标注不一致。"
                ),
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
        calibration_path(task.id).read_text(encoding="utf-8")
    )
    return LabeledCalibrationJudge(
        [
            (criterion_id, verdict)
            for fixture in payload.fixtures
            for criterion_id, verdict in _calibration_judge_verdicts(
                fixture.judge_verdicts
            )
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
    if isinstance(value, str) and value.startswith("__TOMORROW__"):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        return tomorrow + value.removeprefix("__TOMORROW__")
    if isinstance(value, list):
        return [_replace_tomorrow(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tomorrow(item) for key, item in value.items()}
    return value
