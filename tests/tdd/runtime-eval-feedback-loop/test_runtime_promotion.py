from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditIssue,
    IssueRegistry,
)
from evals.release_review.contracts import (
    ReleaseScenario,
    RuntimeScenarioProvenance,
)
from evals.release_review.cli import main as release_review_main
from evals.release_review.loader import load_scenario
from evals.release_review.runtime_promotion import (
    discover_runtime_candidates,
    promote_runtime_candidate,
)


def _issue(
    issue_key: str,
    *,
    status: str = "open",
    trace_evidence_refs: list[str] | None = None,
) -> DailyAuditIssue:
    return DailyAuditIssue(
        issue_key=issue_key,
        status=status,
        title=f"{issue_key} title",
        plain_summary=f"{issue_key} summary",
        first_seen=date(2026, 8, 6),
        last_seen=date(2026, 8, 9),
        trace_evidence_refs=trace_evidence_refs or [],
    )


def _scenario(*, issue_key: str | None = None) -> ReleaseScenario:
    payload = {
        "id": "runtime_grounding_regression",
        "phase": "decision",
        "capability": "grounded_response",
        "risk": "high",
        "request": "使用工具结果回答测试请求。",
        "tool_contract": {"required": [], "allowed": [], "forbidden": []},
    }
    if issue_key is not None:
        payload["provenance"] = RuntimeScenarioProvenance(
            source="runtime_audit",
            issue_key=issue_key,
            first_seen=date(2026, 8, 6),
            last_seen=date(2026, 8, 9),
            evidence_sha256="a" * 64,
            reviewed_by="operator-1",
            reviewed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        ).model_dump(mode="python")
    return ReleaseScenario.model_validate(payload)


def test_discovers_only_trace_backed_actionable_unpromoted_issues() -> None:
    registry = IssueRegistry(
        issues={
            "open_issue": _issue(
                "open_issue",
                trace_evidence_refs=["trace:trace-1/score:score-1"],
            ),
            "addressed_issue": _issue(
                "addressed_issue",
                status="code_addressed",
                trace_evidence_refs=["trace:trace-2"],
            ),
            "regressed_issue": _issue(
                "regressed_issue",
                status="regressed",
                trace_evidence_refs=["trace:trace-3"],
            ),
            "uncertain_issue": _issue(
                "uncertain_issue",
                status="uncertain",
                trace_evidence_refs=["trace:trace-4"],
            ),
            "verified_issue": _issue(
                "verified_issue",
                status="runtime_verified",
                trace_evidence_refs=["trace:trace-5"],
            ),
            "no_trace_issue": _issue("no_trace_issue"),
            "already_promoted": _issue(
                "already_promoted",
                trace_evidence_refs=["trace:trace-6"],
            ),
        }
    )

    candidates = discover_runtime_candidates(
        registry,
        existing_scenarios=(_scenario(issue_key="already_promoted"),),
    )

    assert [item.issue_key for item in candidates] == [
        "addressed_issue",
        "open_issue",
        "regressed_issue",
    ]
    assert candidates[1].trace_evidence_count == 1
    assert candidates[1].first_seen == date(2026, 8, 6)
    assert candidates[1].last_seen == date(2026, 8, 9)


def _write_draft(path: Path, *, phase: str = "decision") -> None:
    payload: dict[str, object] = {
        "id": "route_answer_must_not_invent_details",
        "phase": phase,
        "capability": "grounded_route_answer",
        "risk": "high",
        "request": "根据测试路线工具的实际结果回答，不确定的信息明确说明未知。",
        "tool_contract": {
            "required": ["probe.route"],
            "allowed": [],
            "forbidden": [],
            "sequence": {"before_final_response": ["probe.route"]},
        },
        "fixtures": {
            "probe.route": [
                {"success": True, "data": {"route": ["测试线路"]}}
            ]
        },
        "state_assertions": [{"path": "status", "equals": "completed"}],
    }
    if phase == "staging":
        payload["fixtures"] = {}
        payload["staging"] = {
            "resource_profile": "amap_readonly",
            "cleanup": "skipped",
        }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def _promotion_registry() -> IssueRegistry:
    issue = _issue(
        "route_grounding",
        trace_evidence_refs=["trace:secret-trace-id/score:score-1"],
    )
    return IssueRegistry(issues={issue.issue_key: issue})


def _write_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_promotion_registry().model_dump_json(indent=2), encoding="utf-8")


def test_promotes_reviewed_decision_without_persisting_trace_ids(tmp_path: Path) -> None:
    draft = tmp_path / "draft.yaml"
    scenario_root = tmp_path / "scenarios"
    _write_draft(draft)

    result = promote_runtime_candidate(
        registry=_promotion_registry(),
        existing_scenarios=(),
        scenario_root=scenario_root,
        issue_key="route_grounding",
        draft_path=draft,
        operator="operator-1",
        allow_write=True,
        reviewed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    assert result.path == scenario_root / "runtime" / f"{result.scenario.id}.yaml"
    assert result.path.exists()
    assert "secret-trace-id" not in result.path.read_text(encoding="utf-8")
    loaded = load_scenario(result.path)
    assert loaded.provenance is not None
    assert loaded.provenance.issue_key == "route_grounding"
    assert loaded.provenance.reviewed_by == "operator-1"
    assert loaded.provenance.evidence_sha256 == result.evidence_sha256


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"allow_write": False}, "allow_write"),
        ({"issue_key": "missing"}, "unknown runtime audit issue"),
        ({"operator": "unsafe operator"}, "operator"),
    ],
)
def test_promotion_rejects_missing_review_authority(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    draft = tmp_path / "draft.yaml"
    _write_draft(draft)
    kwargs: dict[str, object] = {
        "registry": _promotion_registry(),
        "existing_scenarios": (),
        "scenario_root": tmp_path / "scenarios",
        "issue_key": "route_grounding",
        "draft_path": draft,
        "operator": "operator-1",
        "allow_write": True,
    }
    kwargs.update(change)

    with pytest.raises((ValueError, PermissionError), match=message):
        promote_runtime_candidate(**kwargs)


def test_promotion_rejects_staging_duplicate_and_target_conflicts(tmp_path: Path) -> None:
    staging = tmp_path / "staging.yaml"
    _write_draft(staging, phase="staging")
    common = {
        "registry": _promotion_registry(),
        "scenario_root": tmp_path / "scenarios",
        "issue_key": "route_grounding",
        "operator": "operator-1",
        "allow_write": True,
    }

    with pytest.raises(ValueError, match="Decision"):
        promote_runtime_candidate(
            **common,
            existing_scenarios=(),
            draft_path=staging,
        )

    draft = tmp_path / "draft.yaml"
    _write_draft(draft)
    with pytest.raises(ValueError, match="already promoted"):
        promote_runtime_candidate(
            **common,
            existing_scenarios=(_scenario(issue_key="route_grounding"),),
            draft_path=draft,
        )

    target = tmp_path / "scenarios" / "runtime" / "route_answer_must_not_invent_details.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        promote_runtime_candidate(
            **common,
            existing_scenarios=(),
            draft_path=draft,
        )


def test_cli_lists_runtime_candidates_without_loading_provider_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry_path = tmp_path / "issues.json"
    scenario_root = tmp_path / "scenarios"
    _write_registry(registry_path)

    exit_code = release_review_main(
        [
            "--list-runtime-candidates",
            "--runtime-issue-registry",
            str(registry_path),
            "--scenario-root",
            str(scenario_root),
        ]
    )

    assert exit_code == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["action"] == "list_runtime_candidates"
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["issue_key"] == "route_grounding"


def test_cli_promotes_candidate_only_with_explicit_write_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry_path = tmp_path / "issues.json"
    scenario_root = tmp_path / "scenarios"
    draft = tmp_path / "draft.yaml"
    _write_registry(registry_path)
    _write_draft(draft)
    base_args = [
        "--promote-runtime-candidate",
        "--runtime-issue-registry",
        str(registry_path),
        "--scenario-root",
        str(scenario_root),
        "--issue-key",
        "route_grounding",
        "--draft-scenario",
        str(draft),
        "--operator",
        "operator-1",
    ]

    with pytest.raises(SystemExit) as exc_info:
        release_review_main(base_args)
    assert exc_info.value.code == 2

    exit_code = release_review_main([*base_args, "--allow-write-scenario"])

    assert exit_code == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["action"] == "promote_runtime_candidate"
    assert payload["issue_key"] == "route_grounding"
    assert Path(payload["scenario_path"]).exists()
