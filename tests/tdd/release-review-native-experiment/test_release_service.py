from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from evals.release_review.cli import main
from evals.release_review.report import (
    ApprovedBaseline,
    ReleaseItemAssessment,
    build_release_report,
)
from evals.release_review.service import (
    ReleaseReviewRequest,
    ReleaseReviewService,
)


def _assessment(
    scenario_id: str,
    *,
    repetition: int = 1,
    risk: str = "high",
    conformance: bool | None = True,
    infrastructure_status: str | None = None,
) -> ReleaseItemAssessment:
    scores = (
        {
            "assistant_agent.quality.task_conformance": conformance,
            "assistant_agent.quality.grounding": True,
            "assistant_agent.quality.response_quality": True,
        }
        if conformance is not None
        else {}
    )
    return ReleaseItemAssessment(
        scenario_id=scenario_id,
        repetition=repetition,
        phase="decision",
        risk=risk,
        scenario_hash=f"hash-{scenario_id}",
        trace_id=f"trace-{scenario_id}-{repetition}",
        scores=scores,
        infrastructure_status=infrastructure_status,
    )


def test_report_classifies_critical_high_flaky_and_infrastructure() -> None:
    report = build_release_report(
        release_id="release-1",
        experiment_run_id="run-1",
        experiment_run_url="https://langfuse.invalid/run-1",
        model="model",
        prompt_version="prompt",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        assessments=(
            _assessment("critical", repetition=1, risk="critical", conformance=True),
            _assessment("critical", repetition=2, risk="critical", conformance=False),
            _assessment("high", risk="high", conformance=False),
            _assessment("infra", conformance=None, infrastructure_status="provider_timeout"),
        ),
    )

    assert report.risks.critical == ("critical",)
    assert report.risks.high == ("high",)
    assert report.risks.flaky == ("critical",)
    assert report.risks.infrastructure == ("infra:provider_timeout",)
    assert report.status == "infrastructure_failed"


def test_baseline_comparison_requires_matching_contract_identity() -> None:
    assessments = (_assessment("high"),)
    matching = ApprovedBaseline(
        experiment_run_id="approved-run",
        prompt_version="prompt",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        scenario_hashes={"high": "hash-high"},
    )
    changed = matching.model_copy(update={"catalog_generation": "different"})

    comparable = build_release_report(
        release_id="release-1",
        experiment_run_id="run-1",
        experiment_run_url=None,
        model="model",
        prompt_version="prompt",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        assessments=assessments,
        baseline=matching,
    )
    incomparable = build_release_report(
        release_id="release-1",
        experiment_run_id="run-1",
        experiment_run_url=None,
        model="model",
        prompt_version="prompt",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        assessments=assessments,
        baseline=changed,
    )

    assert comparable.baseline.comparable is True
    assert incomparable.baseline.comparable is False
    assert "catalog_generation" in incomparable.baseline.reason


def test_service_runs_fixed_pipeline_and_marks_global_timeout(tmp_path: Path) -> None:
    times = iter((0.0, 10.0, 571.0))
    calls = []
    scenarios = (SimpleNamespace(id="high"),)
    experiment = SimpleNamespace(
        run_name="run-1",
        dataset_run_id="run-id-1",
        dataset_run_url="https://langfuse.invalid/run-id-1",
    )
    settings = SimpleNamespace(
        release_id="release-1",
        model="model",
        prompt_version="prompt",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
    )

    service = ReleaseReviewService(
        client=SimpleNamespace(flush=lambda: calls.append("flush")),
        scenario_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        load_scenarios_fn=lambda root: scenarios,
        settings_factory=lambda request, selected: settings,
        sync_dataset_fn=lambda client, selected, commit: calls.append("sync"),
        experiment_runner=lambda client, selected, built: experiment,
        score_auditor=lambda client, native, selected: (_assessment("high"),),
        monotonic_fn=lambda: next(times),
    )

    report = service.run(
        ReleaseReviewRequest(
            release_id="release-1", model="model", prompt_version="prompt"
        )
    )

    assert calls == ["sync", "flush"]
    assert report.elapsed_seconds == 571.0
    assert "release_review:global_timeout" in report.risks.infrastructure
    assert (tmp_path / "artifacts" / "release-1" / "report.json").exists()
    assert (tmp_path / "artifacts" / "release-1" / "report.md").exists()


def test_service_records_only_safe_human_decision_fields(tmp_path: Path) -> None:
    service = ReleaseReviewService.for_decisions_only(tmp_path)

    record = service.record_release_decision(
        release_id="release-1",
        experiment_run_id="run-1",
        decision="approved_with_risk",
        operator="operator-1",
        note="Known staging latency risk.",
    )

    assert record.decision == "approved_with_risk"
    payload = (tmp_path / "release-1" / "decision.json").read_text(encoding="utf-8")
    assert "approved_with_risk" in payload
    assert "provider_response" not in payload


def test_cli_inspect_is_offline_and_record_decision_is_explicit(
    tmp_path: Path, capsys
) -> None:
    scenario_root = tmp_path / "scenarios"
    scenario_root.mkdir()

    assert main(["--inspect", "--scenario-root", str(scenario_root)]) == 0
    assert '"scenario_count": 0' in capsys.readouterr().out

    assert (
        main(
            [
                "--record-decision",
                "--artifact-root",
                str(tmp_path / "artifacts"),
                "--release-id",
                "release-1",
                "--experiment-run-id",
                "run-1",
                "--decision",
                "approved",
                "--operator",
                "operator-1",
            ]
        )
        == 0
    )
    assert (tmp_path / "artifacts" / "release-1" / "decision.json").exists()
