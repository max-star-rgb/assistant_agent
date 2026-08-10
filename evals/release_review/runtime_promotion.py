from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from assistant_agent.observability.runtime_audit.daily_models import IssueRegistry

from .contracts import ReleaseScenario


class RuntimeRegressionCandidate(BaseModel):
    """Prompt-safe operator view of one runtime issue eligible for promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_key: str
    status: Literal["open", "code_addressed", "regressed"]
    title: str
    plain_summary: str
    first_seen: date
    last_seen: date
    trace_evidence_count: int


def discover_runtime_candidates(
    registry: IssueRegistry,
    *,
    existing_scenarios: Sequence[ReleaseScenario] = (),
) -> tuple[RuntimeRegressionCandidate, ...]:
    promoted_issue_keys = {
        scenario.provenance.issue_key
        for scenario in existing_scenarios
        if scenario.provenance is not None
        and scenario.provenance.source == "runtime_audit"
    }
    candidates = []
    for issue_key, issue in sorted(registry.issues.items()):
        if issue.status not in {"open", "code_addressed", "regressed"}:
            continue
        if not issue.trace_evidence_refs or issue_key in promoted_issue_keys:
            continue
        candidates.append(
            RuntimeRegressionCandidate(
                issue_key=issue_key,
                status=issue.status,
                title=issue.title,
                plain_summary=issue.plain_summary,
                first_seen=issue.first_seen,
                last_seen=issue.last_seen,
                trace_evidence_count=len(issue.trace_evidence_refs),
            )
        )
    return tuple(candidates)
