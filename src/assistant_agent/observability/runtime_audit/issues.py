"""Issue lifecycle validation for daily runtime audits."""

from __future__ import annotations

from datetime import date

from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditIssue,
    IssueRegistry,
    IssueStatus,
)


_ALLOWED_TRANSITIONS: dict[IssueStatus, set[IssueStatus]] = {
    "open": {"open", "code_addressed", "uncertain"},
    "code_addressed": {"code_addressed", "runtime_verified", "regressed", "uncertain"},
    "runtime_verified": {"runtime_verified", "regressed"},
    "regressed": {"regressed", "code_addressed", "uncertain"},
    "uncertain": {"uncertain", "open", "code_addressed"},
}
_INITIAL_STATUSES = {"open", "uncertain"}


def merge_issue_registry(
    previous: IssueRegistry,
    observed: list[DailyAuditIssue],
    audit_date: date,
) -> IssueRegistry:
    """Merge daily observations without promoting unsupported issue states."""

    merged = dict(previous.issues)
    observed_keys: set[str] = set()
    for candidate in observed:
        if candidate.issue_key in observed_keys:
            raise ValueError(f"duplicate observed issue: {candidate.issue_key}")
        observed_keys.add(candidate.issue_key)
        current = previous.issues.get(candidate.issue_key)
        _validate_observation(current, candidate)
        merged[candidate.issue_key] = _merge_issue(current, candidate, audit_date)
    return IssueRegistry(issues=merged)


def _validate_observation(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
) -> None:
    if current is None:
        if candidate.status not in _INITIAL_STATUSES:
            raise ValueError("new issues must start open or uncertain")
    elif candidate.status not in _ALLOWED_TRANSITIONS[current.status]:
        raise ValueError(
            f"invalid issue transition: {current.status} -> {candidate.status}"
        )

    if candidate.status == "code_addressed" and not candidate.code_evidence_refs:
        raise ValueError("code_addressed requires code evidence")
    if candidate.status == "runtime_verified":
        _require_runtime_verification_evidence(current, candidate)
    if candidate.status == "regressed":
        _require_new_trace_evidence(current, candidate)


def _require_runtime_verification_evidence(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
) -> None:
    if not candidate.runtime_verification_refs:
        raise ValueError("runtime_verified requires runtime verification evidence")
    if current is not None and not _new_refs(
        candidate.runtime_verification_refs, _all_evidence_refs(current)
    ):
        raise ValueError("runtime_verified requires subsequent runtime verification evidence")


def _require_new_trace_evidence(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
) -> None:
    if current is None or not _new_refs(
        candidate.trace_evidence_refs, _all_evidence_refs(current)
    ):
        raise ValueError("regressed requires new trace evidence")


def _merge_issue(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
    audit_date: date,
) -> DailyAuditIssue:
    if current is None:
        return candidate.model_copy(
            update={"first_seen": audit_date, "last_seen": audit_date}
        )
    return candidate.model_copy(
        update={
            "first_seen": current.first_seen,
            "last_seen": audit_date,
            "trace_evidence_refs": _merge_refs(
                current.trace_evidence_refs, candidate.trace_evidence_refs
            ),
            "code_evidence_refs": _merge_refs(
                current.code_evidence_refs, candidate.code_evidence_refs
            ),
            "runtime_verification_refs": _merge_refs(
                current.runtime_verification_refs,
                candidate.runtime_verification_refs,
            ),
        }
    )


def _all_evidence_refs(issue: DailyAuditIssue) -> set[str]:
    return set(
        issue.trace_evidence_refs
        + issue.code_evidence_refs
        + issue.runtime_verification_refs
    )


def _new_refs(candidate_refs: list[str], existing_refs: set[str]) -> set[str]:
    return set(candidate_refs) - existing_refs


def _merge_refs(existing: list[str], observed: list[str]) -> list[str]:
    return list(dict.fromkeys([*existing, *observed]))
