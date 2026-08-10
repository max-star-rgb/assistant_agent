from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CANONICAL_TASK_SCORES = (
    "assistant_agent.quality.task_conformance",
    "assistant_agent.quality.grounding",
    "assistant_agent.quality.response_quality",
)


class ReleaseItemAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    repetition: int = Field(ge=1)
    phase: Literal["decision", "staging"]
    risk: Literal["critical", "high", "medium", "low"]
    scenario_hash: str
    trace_id: str | None = None
    scores: dict[str, bool] = Field(default_factory=dict)
    infrastructure_status: str | None = None


class ApprovedBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_run_id: str
    model: str
    catalog_generation: str
    evaluator_version: str
    scenario_hashes: dict[str, str]


class BaselineComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_run_id: str | None = None
    comparable: bool
    reason: str


class ReleaseRiskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    critical: tuple[str, ...] = ()
    high: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    infrastructure: tuple[str, ...] = ()


class ReleaseReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_id: str
    experiment_run_id: str
    experiment_run_url: str | None = None
    model: str
    git_commit: str
    catalog_generation: str
    evaluator_version: str
    elapsed_seconds: float = Field(default=0.0, ge=0)
    status: Literal["ready_for_decision", "infrastructure_failed"]
    assessments: tuple[ReleaseItemAssessment, ...]
    risks: ReleaseRiskSummary
    baseline: BaselineComparison

    def to_markdown(self) -> str:
        return "\n".join(
            (
                f"# Release Review {self.release_id}",
                "",
                f"- Experiment Run: {self.experiment_run_id}",
                f"- Status: {self.status}",
                f"- Critical: {', '.join(self.risks.critical) or 'none'}",
                f"- High: {', '.join(self.risks.high) or 'none'}",
                f"- Flaky: {', '.join(self.risks.flaky) or 'none'}",
                f"- Infrastructure: {', '.join(self.risks.infrastructure) or 'none'}",
                f"- Baseline: {self.baseline.reason}",
                "",
            )
        )


def build_release_report(
    *,
    release_id: str,
    experiment_run_id: str,
    experiment_run_url: str | None,
    model: str,
    git_commit: str,
    catalog_generation: str,
    evaluator_version: str,
    assessments: tuple[ReleaseItemAssessment, ...],
    baseline: ApprovedBaseline | None = None,
    elapsed_seconds: float = 0.0,
    additional_infrastructure: tuple[str, ...] = (),
) -> ReleaseReviewReport:
    critical: set[str] = set()
    high: set[str] = set()
    infrastructure = set(additional_infrastructure)
    outcomes: dict[str, list[bool]] = defaultdict(list)
    scenario_hashes: dict[str, str] = {}
    for item in assessments:
        scenario_hashes[item.scenario_id] = item.scenario_hash
        if item.infrastructure_status is not None:
            infrastructure.add(f"{item.scenario_id}:{item.infrastructure_status}")
            continue
        missing = [name for name in CANONICAL_TASK_SCORES if name not in item.scores]
        if missing:
            infrastructure.add(
                f"{item.scenario_id}:missing_scores:{','.join(sorted(missing))}"
            )
            continue
        passed = all(item.scores[name] for name in CANONICAL_TASK_SCORES)
        outcomes[item.scenario_id].append(passed)
        if not passed and item.risk == "critical":
            critical.add(item.scenario_id)
        elif not passed and item.risk == "high":
            high.add(item.scenario_id)
    flaky = {
        scenario_id
        for scenario_id, values in outcomes.items()
        if len(values) > 1 and len(set(values)) > 1
    }
    comparison = _compare_baseline(
        baseline,
        model=model,
        catalog_generation=catalog_generation,
        evaluator_version=evaluator_version,
        scenario_hashes=scenario_hashes,
    )
    risks = ReleaseRiskSummary(
        critical=tuple(sorted(critical)),
        high=tuple(sorted(high)),
        flaky=tuple(sorted(flaky)),
        infrastructure=tuple(sorted(infrastructure)),
    )
    return ReleaseReviewReport(
        release_id=release_id,
        experiment_run_id=experiment_run_id,
        experiment_run_url=experiment_run_url,
        model=model,
        git_commit=git_commit,
        catalog_generation=catalog_generation,
        evaluator_version=evaluator_version,
        elapsed_seconds=elapsed_seconds,
        status=(
            "infrastructure_failed"
            if risks.infrastructure
            else "ready_for_decision"
        ),
        assessments=assessments,
        risks=risks,
        baseline=comparison,
    )


def _compare_baseline(
    baseline: ApprovedBaseline | None,
    *,
    model: str,
    catalog_generation: str,
    evaluator_version: str,
    scenario_hashes: dict[str, str],
) -> BaselineComparison:
    if baseline is None:
        return BaselineComparison(comparable=False, reason="no approved baseline")
    mismatches: list[str] = []
    if baseline.model != model:
        mismatches.append("model")
    if baseline.catalog_generation != catalog_generation:
        mismatches.append("catalog_generation")
    if baseline.evaluator_version != evaluator_version:
        mismatches.append("evaluator_version")
    if baseline.scenario_hashes != scenario_hashes:
        mismatches.append("scenario_hashes")
    if mismatches:
        return BaselineComparison(
            baseline_run_id=baseline.experiment_run_id,
            comparable=False,
            reason="incomparable: " + ", ".join(mismatches),
        )
    return BaselineComparison(
        baseline_run_id=baseline.experiment_run_id,
        comparable=True,
        reason="comparable to approved baseline",
    )
