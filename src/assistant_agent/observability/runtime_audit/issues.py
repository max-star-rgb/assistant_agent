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
_INITIAL_STATUSES = {"open", "code_addressed", "uncertain"}


def merge_issue_registry(
    previous: IssueRegistry,
    observed: list[DailyAuditIssue],
    audit_date: date,
) -> IssueRegistry:
    """Merge daily observations without promoting unsupported issue states."""

    previous = IssueRegistry.model_validate(previous.model_dump(mode="python"))
    merged = dict(previous.issues)
    observed_keys: set[str] = set()
    for unvalidated_candidate in observed:
        candidate = DailyAuditIssue.model_validate(
            unvalidated_candidate.model_dump(mode="python")
        )
        if candidate.issue_key in observed_keys:
            raise ValueError(f"duplicate observed issue: {candidate.issue_key}")
        observed_keys.add(candidate.issue_key)
        current = previous.issues.get(candidate.issue_key)
        _validate_observation(current, candidate, audit_date)
        merged[candidate.issue_key] = _merge_issue(current, candidate, audit_date)
    return IssueRegistry(issues=merged)


def build_refresh_issue_registry(
    previous: IssueRegistry,
    observed: list[DailyAuditIssue],
    audit_date: date,
) -> IssueRegistry:
    """Validate a read-only historical refresh without applying chronology persistence."""

    previous = IssueRegistry.model_validate(previous.model_dump(mode="python"))
    merged = dict(previous.issues)
    observed_keys: set[str] = set()
    for unvalidated_candidate in observed:
        candidate = DailyAuditIssue.model_validate(
            unvalidated_candidate.model_dump(mode="python")
        )
        if candidate.issue_key in observed_keys:
            raise ValueError(f"duplicate observed issue: {candidate.issue_key}")
        observed_keys.add(candidate.issue_key)
        current = previous.issues.get(candidate.issue_key)
        if current is not None and current.last_seen > audit_date:
            if candidate.status in {"runtime_verified", "regressed"}:
                raise ValueError(
                    "historical refresh predates the existing issue lifecycle"
                )
            current = None
        _validate_observation(
            current,
            candidate,
            audit_date,
            require_later_date=True,
        )
        merged[candidate.issue_key] = _merge_issue(current, candidate, audit_date)
    return IssueRegistry(issues=merged)


def report_issue_view(
    previous: IssueRegistry,
    merged: IssueRegistry,
    observed: list[DailyAuditIssue],
    *,
    audit_date: date,
) -> list[DailyAuditIssue]:
    """Return deterministic active state plus only genuine observed verifications."""

    observed_by_key = {issue.issue_key: issue for issue in observed}
    visible: list[DailyAuditIssue] = []
    for issue_key, issue in sorted(merged.issues.items()):
        prior = previous.issues.get(issue_key)
        if issue.first_seen > audit_date:
            continue
        if prior is not None and prior.last_seen > audit_date and issue_key not in observed_by_key:
            continue
        if issue.status in {"open", "regressed", "code_addressed", "uncertain"}:
            visible.append(issue)
            continue
        candidate = observed_by_key.get(issue_key)
        if (
            issue.status == "runtime_verified"
            and candidate is not None
            and candidate.status == "runtime_verified"
            and prior is not None
            and prior.status != "runtime_verified"
        ):
            visible.append(issue)
    return visible


def _validate_observation(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
    audit_date: date,
    *,
    require_later_date: bool = True,
) -> None:
    if current is None:
        if candidate.status not in _INITIAL_STATUSES:
            raise ValueError("new issues must start open or uncertain")
    elif candidate.status not in _ALLOWED_TRANSITIONS[current.status]:
        raise ValueError(
            f"invalid issue transition: {current.status} -> {candidate.status}"
        )

    if candidate.status == "code_addressed":
        if not any(ref.startswith("code:") for ref in candidate.code_evidence_refs):
            raise ValueError("code_addressed requires code evidence using code:<commit-sha>")
        if current is None and not candidate.trace_evidence_refs:
            raise ValueError("first-seen code_addressed requires bad trace evidence")
        if (
            current is not None
            and current.status != "code_addressed"
            and not _new_refs(
                [ref for ref in candidate.code_evidence_refs if ref.startswith("code:")],
                {
                    ref
                    for ref in current.code_evidence_refs
                    if ref.startswith("code:")
                },
            )
        ):
            raise ValueError("code_addressed transition requires new code evidence")
    if candidate.status == "runtime_verified":
        _require_runtime_verification_evidence(
            current,
            candidate,
            audit_date,
            require_later_date=require_later_date,
        )
    if candidate.status == "regressed":
        _require_new_trace_evidence(
            current,
            candidate,
            audit_date,
            require_later_date=require_later_date,
        )


def _require_runtime_verification_evidence(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
    audit_date: date,
    *,
    require_later_date: bool,
) -> None:
    if not candidate.runtime_verification_refs:
        raise ValueError("runtime_verified requires runtime verification evidence")
    if require_later_date:
        _require_later_audit_date(current, audit_date)
    if current is not None and not _new_refs(
        candidate.runtime_verification_refs, _trace_evidence_refs(current)
    ):
        raise ValueError("runtime_verified requires subsequent runtime verification evidence")


def _require_new_trace_evidence(
    current: DailyAuditIssue | None,
    candidate: DailyAuditIssue,
    audit_date: date,
    *,
    require_later_date: bool,
) -> None:
    if require_later_date:
        _require_later_audit_date(current, audit_date)
    if current is None or not _new_refs(
        candidate.trace_evidence_refs, _trace_evidence_refs(current)
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


def _require_later_audit_date(current: DailyAuditIssue | None, audit_date: date) -> None:
    if current is None or audit_date <= current.last_seen:
        raise ValueError("status transition must occur after previous last_seen")


def _trace_evidence_refs(issue: DailyAuditIssue) -> set[str]:
    return set(issue.trace_evidence_refs + issue.runtime_verification_refs)


def _new_refs(candidate_refs: list[str], existing_refs: set[str]) -> set[str]:
    return set(candidate_refs) - existing_refs


def _merge_refs(existing: list[str], observed: list[str]) -> list[str]:
    return list(dict.fromkeys([*existing, *observed]))
