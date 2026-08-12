from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .experiment import ReleaseExperimentSettings, run_release_experiment
from .langsmith_backend import (
    REQUIRED_RELEASE_FEEDBACK_KEYS,
    audit_langsmith_feedback,
    sync_langsmith_examples,
    wait_for_langsmith_runs,
)
from .loader import load_scenarios
from .report import (
    ApprovedBaseline,
    ReleaseItemAssessment,
    ReleaseReviewReport,
    LangSmithTargetEvidence,
    build_release_report,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReleaseReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    scenario_ids: tuple[str, ...] | None = None
    run_name: str | None = Field(default=None, max_length=128)


class ReleaseDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_id: str
    experiment_run_id: str
    decision: Literal["approved", "approved_with_risk", "rejected"]
    operator: str
    decided_at: datetime
    note: str = ""


class ReleaseReviewService:
    def __init__(
        self,
        *,
        client: Any | None = None,
        scenario_root: Path | None = None,
        artifact_root: Path = Path(".data/evals/release_review"),
        load_scenarios_fn: Callable[[Path], tuple[Any, ...]] = load_scenarios,
        settings_factory: Callable[
            [ReleaseReviewRequest, tuple[Any, ...]], ReleaseExperimentSettings
        ]
        | None = None,
        sync_examples_fn: Callable[..., Any] = sync_langsmith_examples,
        experiment_runner: Callable[..., Any] = run_release_experiment,
        wait_for_runs_fn: Callable[..., Any] = wait_for_langsmith_runs,
        feedback_auditor: Callable[
            ..., tuple[ReleaseItemAssessment, ...]
        ] = audit_langsmith_feedback,
        baseline_loader: Callable[[Any], ApprovedBaseline | None] | None = None,
        monotonic_fn: Callable[[], float] = monotonic,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.client = client
        self.scenario_root = scenario_root
        self.artifact_root = artifact_root
        self._load_scenarios = load_scenarios_fn
        self._settings_factory = settings_factory
        self._sync_examples = sync_examples_fn
        self._experiment_runner = experiment_runner
        self._wait_for_runs = wait_for_runs_fn
        self._feedback_auditor = feedback_auditor
        self._baseline_loader = baseline_loader
        self._monotonic = monotonic_fn
        self._progress = progress

    @classmethod
    def for_decisions_only(cls, artifact_root: Path) -> "ReleaseReviewService":
        return cls(artifact_root=artifact_root)

    def run(self, request: ReleaseReviewRequest) -> ReleaseReviewReport:
        if (
            self.client is None
            or self.scenario_root is None
            or self._settings_factory is None
        ):
            raise RuntimeError("release review run service is not fully configured")
        started = self._monotonic()
        scenarios = self._load_scenarios(self.scenario_root)
        selected = _select_scenarios(scenarios, request.scenario_ids)
        if self._progress is not None:
            self._progress(
                {
                    "event": "release_review.run.started",
                    "task_count": sum(scenario.repetitions for scenario in selected),
                }
            )
        settings = self._settings_factory(request, selected)
        sync = self._sync_examples(self.client, scenarios, settings.git_commit)
        selected_ids = {scenario.id for scenario in selected}
        selected_bindings = tuple(
            binding for binding in sync.bindings if binding.scenario_id in selected_ids
        )
        preflight_elapsed = self._monotonic() - started
        if preflight_elapsed > 30:
            raise TimeoutError("release review preflight exceeded 30 seconds")
        if self._progress is None:
            experiment = _resolve(
                self._experiment_runner(self.client, selected, settings)
            )
        else:
            experiment = _resolve(
                self._experiment_runner(
                    self.client,
                    selected,
                    settings,
                    progress=self._progress,
                )
            )
        if experiment.dataset_id != sync.dataset_id:
            raise RuntimeError("Release Review Experiment targeted the wrong Dataset")
        if set(experiment.example_ids) != {
            binding.example_id for binding in selected_bindings
        }:
            raise RuntimeError(
                "Release Review Experiment Examples do not match Git bindings"
            )
        self.client.flush()
        completeness = self._wait_for_runs(
            self.client,
            experiment_id=experiment.experiment_id,
            example_ids=experiment.example_ids,
        )
        assessments = self._feedback_auditor(
            completeness,
            selected_bindings,
            selected,
            experiment.cleanup_results,
        )
        elapsed = self._monotonic() - started
        additional = ("release_review:global_timeout",) if elapsed > 570 else ()
        baseline = self._baseline_loader(self.client) if self._baseline_loader else None
        report = build_release_report(
            release_id=request.release_id,
            experiment_run_id=experiment.experiment_id,
            experiment_run_url=experiment.experiment_url,
            model=settings.model,
            git_commit=settings.git_commit,
            catalog_generation=settings.catalog_generation,
            evaluator_version=settings.evaluator_version,
            assessments=assessments,
            baseline=baseline,
            elapsed_seconds=elapsed,
            additional_infrastructure=additional,
            langsmith_evidence=LangSmithTargetEvidence(
                target="release_review",
                dataset_id=experiment.dataset_id,
                project_id=experiment.experiment_id,
                experiment_id=experiment.experiment_id,
                active_example_ids=experiment.example_ids,
                root_run_ids=completeness.root_run_ids,
                required_feedback=REQUIRED_RELEASE_FEEDBACK_KEYS,
                feedback=completeness.feedback,
                native_tree_complete=completeness.native_tree_complete,
                infrastructure_failures=additional,
            ),
        )
        self._write_report(report)
        if self._progress is not None:
            self._progress(
                {
                    "event": "release_review.run.completed",
                    "task_count": len(report.assessments),
                    "infrastructure_issue_count": len(report.risks.infrastructure),
                }
            )
        return report

    def record_release_decision(
        self,
        *,
        release_id: str,
        experiment_run_id: str,
        decision: Literal["approved", "approved_with_risk", "rejected"],
        operator: str,
        note: str = "",
    ) -> ReleaseDecisionRecord:
        for label, value in (
            ("release_id", release_id),
            ("experiment_run_id", experiment_run_id),
            ("operator", operator),
        ):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"{label} must be a safe identifier")
        if len(note) > 2_000:
            raise ValueError("decision note is too long")
        record = ReleaseDecisionRecord(
            release_id=release_id,
            experiment_run_id=experiment_run_id,
            decision=decision,
            operator=operator,
            decided_at=datetime.now(timezone.utc),
            note=note,
        )
        target = self.artifact_root / release_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "decision.json").write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
        return record

    def _write_report(self, report: ReleaseReviewReport) -> None:
        target = self.artifact_root / report.release_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (target / "report.md").write_text(report.to_markdown(), encoding="utf-8")


def _select_scenarios(
    scenarios: tuple[Any, ...], scenario_ids: tuple[str, ...] | None
) -> tuple[Any, ...]:
    if scenario_ids is None:
        return scenarios
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("scenario_ids must be unique")
    by_id = {scenario.id: scenario for scenario in scenarios}
    missing = sorted(set(scenario_ids) - set(by_id))
    if missing:
        raise ValueError(f"unknown release scenarios: {', '.join(missing)}")
    return tuple(by_id[scenario_id] for scenario_id in scenario_ids)


def _resolve(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RuntimeError(
        "synchronous ReleaseReviewService cannot run inside an event loop"
    )
