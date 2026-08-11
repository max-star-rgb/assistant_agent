from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import evals.release_review.cli as release_cli
from assistant_agent.tools.registry import ToolRegistry
from evals.release_review.cli import _selection_requires_staging, main
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
        model="model",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        scenario_hashes={"high": "hash-high"},
    )
    changed = matching.model_copy(update={"catalog_generation": "different"})
    changed_model = matching.model_copy(update={"model": "different-model"})

    comparable = build_release_report(
        release_id="release-1",
        experiment_run_id="run-1",
        experiment_run_url=None,
        model="model",
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
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        assessments=assessments,
        baseline=changed,
    )
    model_incomparable = build_release_report(
        release_id="release-1",
        experiment_run_id="run-1",
        experiment_run_url=None,
        model="model",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        assessments=assessments,
        baseline=changed_model,
    )

    assert comparable.baseline.comparable is True
    assert incomparable.baseline.comparable is False
    assert "catalog_generation" in incomparable.baseline.reason
    assert model_incomparable.baseline.comparable is False
    assert "model" in model_incomparable.baseline.reason


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
        ReleaseReviewRequest(release_id="release-1")
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


def test_staging_permission_gate_only_considers_selected_scenarios() -> None:
    scenarios = (
        SimpleNamespace(id="decision-only", phase="decision"),
        SimpleNamespace(id="staging", phase="staging"),
    )

    assert _selection_requires_staging(scenarios, ("decision-only",)) is False
    assert _selection_requires_staging(scenarios, ("staging",)) is True
    assert _selection_requires_staging(scenarios, None) is True


def test_cli_preflight_checks_catalog_without_creating_langfuse_client(
    monkeypatch, capsys
) -> None:
    required: list[str] = []

    class Catalog:
        generation = "catalog-sentinel"

        def require_tools(self, tool_names) -> None:
            required.extend(tool_names)

    monkeypatch.setattr(
        release_cli.ProviderConfig,
        "from_env",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(release_cli, "_validate_real_config", lambda config: None)
    monkeypatch.setattr(release_cli, "_catalog_snapshot", lambda config: Catalog())
    monkeypatch.setattr(
        release_cli,
        "_langfuse_client",
        lambda: (_ for _ in ()).throw(AssertionError("preflight must stay local")),
    )

    assert (
        main(
            [
                "--preflight",
                "--no-env-file",
                "--release-id",
                "release-1",
                "--allow-real-provider",
                "--allow-staging-side-effects",
                "--scenario",
                "deep_research_admission",
            ]
        )
        == 0
    )

    assert required == ["workflow_submit"]
    assert '"status": "ready"' in capsys.readouterr().out


def test_catalog_probe_runs_in_disposable_working_directory(monkeypatch) -> None:
    starting_directory = Path.cwd()
    probe_directories: list[Path] = []

    class Runtime:
        def __init__(self, config) -> None:
            probe_directories.append(Path.cwd())
            self.registry = ToolRegistry()
            self.registry.seal()

        def close(self) -> None:
            pass

    monkeypatch.setattr(release_cli, "AgentGraphRuntime", Runtime)

    release_cli._catalog_snapshot(SimpleNamespace())

    assert probe_directories[0] != starting_directory
    assert not probe_directories[0].exists()
    assert Path.cwd() == starting_directory


def test_release_review_builds_items_through_experiment_runtime_host(monkeypatch) -> None:
    assert hasattr(release_cli, "_create_item_runtime")
    captured = {}

    class Runtime:
        def __init__(
            self,
            *,
            registry,
            config,
            tool_execution_backend,
            trace_store,
        ) -> None:
            captured.update(
                registry=registry,
                config=config,
                backend=tool_execution_backend,
                trace_store=trace_store,
            )

    def create_host(builder):
        captured["runtime"] = builder("trace-store-sentinel")
        return "host-sentinel"

    monkeypatch.setattr(release_cli, "AgentGraphRuntime", Runtime)
    monkeypatch.setattr(release_cli, "create_experiment_runtime_host", create_host)
    config = SimpleNamespace()
    registry = SimpleNamespace()
    backend = SimpleNamespace()

    assert (
        release_cli._create_item_runtime(
            config=config,
            registry=registry,
            backend=backend,
        )
        == "host-sentinel"
    )
    assert captured["registry"] is registry
    assert captured["config"] is config
    assert captured["backend"] is backend
    assert captured["trace_store"] == "trace-store-sentinel"
    assert isinstance(captured["runtime"], Runtime)
