from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict
import yaml

from assistant_agent.observability.runtime_audit.daily_models import IssueRegistry

from .contracts import ReleaseScenario, RuntimeScenarioProvenance
from .loader import load_scenario


_SAFE_OPERATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


class RuntimePromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    path: Path
    scenario: ReleaseScenario
    evidence_sha256: str


def discover_runtime_candidates(
    registry: IssueRegistry,
    *,
    existing_scenarios: Sequence[ReleaseScenario] = (),
) -> tuple[RuntimeRegressionCandidate, ...]:
    promoted_issue_hashes = {
        scenario.provenance.issue_key_sha256
        for scenario in existing_scenarios
        if scenario.provenance is not None
        and scenario.provenance.source == "runtime_audit"
    }
    candidates = []
    for issue_key, issue in sorted(registry.issues.items()):
        if issue.status not in {"open", "code_addressed", "regressed"}:
            continue
        if not issue.trace_evidence_refs or _issue_key_sha256(issue_key) in promoted_issue_hashes:
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


def promote_runtime_candidate(
    *,
    registry: IssueRegistry,
    existing_scenarios: Sequence[ReleaseScenario],
    scenario_root: Path,
    issue_key: str,
    draft_path: Path,
    operator: str,
    allow_write: bool,
    reviewed_at: datetime | None = None,
) -> RuntimePromotionResult:
    """Validate and atomically promote one reviewed runtime issue into Git scenarios."""

    if not allow_write:
        raise PermissionError("allow_write must be explicitly enabled")
    if not _SAFE_OPERATOR.fullmatch(operator):
        raise ValueError("operator must be a safe identifier")
    issue = registry.issues.get(issue_key)
    if issue is None:
        raise ValueError(f"unknown runtime audit issue: {issue_key}")
    if issue.status not in {"open", "code_addressed", "regressed"}:
        raise ValueError(f"runtime audit issue is not actionable: {issue_key}")
    if not issue.trace_evidence_refs:
        raise ValueError(f"runtime audit issue has no trace evidence: {issue_key}")
    if any(
        scenario.provenance is not None
        and scenario.provenance.issue_key_sha256 == _issue_key_sha256(issue_key)
        for scenario in existing_scenarios
    ):
        raise ValueError(f"runtime audit issue already promoted: {issue_key}")

    scenario_root = Path(scenario_root).resolve()
    draft_path = Path(draft_path).resolve()
    if draft_path.is_relative_to(scenario_root):
        raise ValueError("runtime promotion draft must stay outside the formal scenario root")
    draft = load_scenario(draft_path)
    if draft.phase != "decision":
        raise ValueError("runtime promotion only accepts Decision scenarios")
    if draft.provenance is not None:
        raise ValueError("runtime promotion draft must not define provenance")
    if any(scenario.id == draft.id for scenario in existing_scenarios):
        raise ValueError(f"Release Review scenario id already exists: {draft.id}")
    _reject_trace_ids_in_draft(draft, issue.trace_evidence_refs)

    evidence_sha256 = hashlib.sha256(
        json.dumps(
            sorted(issue.trace_evidence_refs),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    scenario = draft.model_copy(
        update={
            "provenance": RuntimeScenarioProvenance(
                source="runtime_audit",
                issue_key_sha256=_issue_key_sha256(issue.issue_key),
                first_seen=issue.first_seen,
                last_seen=issue.last_seen,
                evidence_sha256=evidence_sha256,
                reviewed_by=operator,
                reviewed_at=reviewed_at or datetime.now(timezone.utc),
            )
        }
    )
    target = scenario_root / "runtime" / f"{scenario.id}.yaml"
    if target.exists():
        raise FileExistsError(f"Release Review scenario already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        scenario.model_dump(mode="json", exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )
    _atomic_write_new(target, payload)
    return RuntimePromotionResult(
        path=target,
        scenario=scenario,
        evidence_sha256=evidence_sha256,
    )


def _reject_trace_ids_in_draft(
    scenario: ReleaseScenario,
    evidence_refs: Sequence[str],
) -> None:
    serialized = json.dumps(
        scenario.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
    )
    trace_ids = {
        reference.removeprefix("trace:").split("/", 1)[0]
        for reference in evidence_refs
        if reference.startswith("trace:")
    }
    if any(trace_id and trace_id in serialized for trace_id in trace_ids):
        raise ValueError("runtime promotion draft contains a production trace id")


def _issue_key_sha256(issue_key: str) -> str:
    return hashlib.sha256(issue_key.encode("utf-8")).hexdigest()


def _atomic_write_new(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError(f"Release Review scenario already exists: {path}")
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
