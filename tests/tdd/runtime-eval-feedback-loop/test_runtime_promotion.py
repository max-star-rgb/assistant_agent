from __future__ import annotations

from datetime import date, datetime, timezone

from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditIssue,
    IssueRegistry,
)
from evals.release_review.contracts import (
    ReleaseScenario,
    RuntimeScenarioProvenance,
)
from evals.release_review.runtime_promotion import discover_runtime_candidates


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
