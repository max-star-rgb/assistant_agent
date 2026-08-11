"""Resumable, read-only orchestration for one Shanghai-calendar audit day."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
import re
from pathlib import Path, PurePosixPath
import subprocess
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from assistant_agent.observability.runtime_audit.collector import (
    DEFAULT_LOW_SCORE_THRESHOLD,
    LangfuseAuditSource,
    collect_runtime_audit,
)
from assistant_agent.observability.runtime_audit.codex_input import (
    build_daily_codex_input,
    requires_codex_audit,
)
from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditAttempt,
    DailyAuditCommitIntent,
    DailyCodexAuditReport,
    IssueRegistry,
)
from assistant_agent.observability.runtime_audit.daily_window import (
    DailyAuditWindow,
    pending_audit_dates,
    window_for_date,
)
from assistant_agent.observability.runtime_audit.issues import (
    build_refresh_issue_registry,
    merge_issue_registry,
    report_issue_view,
)
from assistant_agent.observability.runtime_audit.git_evidence import (
    collect_repository_change_evidence,
)
from assistant_agent.observability.runtime_audit.models import RuntimeAuditBundle
from assistant_agent.observability.runtime_audit.report import (
    render_daily_codex_report,
    render_empty_daily_report,
    render_failed_daily_report,
    render_no_anomaly_daily_report,
)
from assistant_agent.observability.runtime_audit.storage import (
    RuntimeAuditArtifactStore,
    registry_digest as storage_registry_digest,
)
from assistant_agent.observability.runtime_audit.safety import (
    sanitize_runtime_audit_text,
)
from assistant_agent.observability.trace_ledger import (
    LEDGER_TIMEZONE,
    prune_trace_ledger,
)


DEFAULT_LOCAL_LEDGER_RETENTION_DAYS = 14


class DailyCommitIntentRejected(RuntimeError):
    """A journal cannot be safely applied to the current persisted state."""


class DailyAuditRunResult(BaseModel):
    audit_date: date
    status: Literal["succeeded", "failed"]
    attempt_id: str
    bundle_path: Path
    report_path: Path | None = None
    error_summary: str | None = None
    retention_warning: str | None = None

    @field_validator("error_summary")
    @classmethod
    def _safe_error(cls, value: str | None) -> str | None:
        return None if value is None else sanitize_runtime_audit_text(value)


class DailyAuditDayError(RuntimeError):
    """An unexpected automatic-audit exception tied to one exact audit day."""

    def __init__(
        self,
        *,
        audit_date: date,
        completed_results: list[DailyAuditRunResult],
    ) -> None:
        super().__init__(f"daily audit execution failed for {audit_date.isoformat()}")
        self.audit_date = audit_date
        self.completed_results = list(completed_results)


def run_one_daily_audit(
    *,
    window: DailyAuditWindow,
    source: LangfuseAuditSource,
    local_trace_path: Path,
    store: RuntimeAuditArtifactStore,
    repo_root: Path,
    codex_runner: Callable[..., DailyCodexAuditReport],
    collected_at: datetime,
    judge_grace: timedelta = timedelta(minutes=15),
    low_score_threshold: float = DEFAULT_LOW_SCORE_THRESHOLD,
    local_ledger_retention_days: int = DEFAULT_LOCAL_LEDGER_RETENTION_DAYS,
    commit_continuous_state: bool = True,
    _claimed: bool = False,
) -> DailyAuditRunResult:
    """Collect, publish, and checkpoint one day without crossing a failed state."""

    claim = nullcontext() if _claimed else store.daily_claim()
    with claim:
        _recover_pending_commits(store)
        result = _run_one_locked(
            window=window, source=source, local_trace_path=local_trace_path,
            store=store, repo_root=repo_root, codex_runner=codex_runner,
            collected_at=collected_at, judge_grace=judge_grace,
            low_score_threshold=low_score_threshold,
            commit_continuous_state=commit_continuous_state,
        )
        if result.status == "succeeded":
            try:
                prune_trace_ledger(
                    local_trace_path,
                    retention_days=local_ledger_retention_days,
                    reference_date=collected_at.astimezone(LEDGER_TIMEZONE).date(),
                    is_day_completed=store.has_successful_day_evidence,
                    approved_snapshots=(
                        store.successful_ledger_partition_snapshots()
                    ),
                )
            except Exception as exc:
                result = result.model_copy(
                    update={"retention_warning": sanitize_runtime_audit_text(exc)}
                )
        return result


def _run_one_locked(
    *, window: DailyAuditWindow, source: LangfuseAuditSource, local_trace_path: Path,
    store: RuntimeAuditArtifactStore, repo_root: Path,
    codex_runner: Callable[..., DailyCodexAuditReport], collected_at: datetime,
    judge_grace: timedelta, low_score_threshold: float,
    commit_continuous_state: bool,
) -> DailyAuditRunResult:
    predecessor = store.last_completed_date()
    if (
        commit_continuous_state
        and predecessor is not None
        and window.audit_date <= predecessor
    ):
        raise ValueError("daily commit target must be newer than its predecessor")
    attempt_id = store.allocate_audit_run_id(collected_at)
    bundle_path = store.staged_daily_bundle_path(attempt_id)
    running = DailyAuditAttempt(
        attempt_id=attempt_id,
        audit_date=window.audit_date,
        status="running",
        bundle_path=str(bundle_path),
        codex_output_path=str(store.codex_json_path(attempt_id)),
    )
    try:
        bundle = collect_runtime_audit(
            source=source, local_trace_path=local_trace_path,
            window_start=window.start_utc, window_end=window.end_utc,
            collected_at=collected_at, audit_run_id=attempt_id,
            judge_grace=judge_grace, low_score_threshold=low_score_threshold,
        )
        bundle_path = store.write_staged_daily_bundle(bundle)
        repository_changes = collect_repository_change_evidence(
            repo_root=repo_root,
            window_start=bundle.window_start,
            collected_at=bundle.collected_at,
        )
        codex_input_path = store.write_staged_daily_codex_input(
            attempt_id,
            build_daily_codex_input(
                bundle,
                repository_changes=repository_changes,
            ),
        )
        running = running.model_copy(
            update={
                "bundle_path": str(bundle_path),
                "codex_input_path": str(codex_input_path),
            }
        )
        store.write_attempt(running)
    except Exception as exc:
        return _fail(store=store, attempt=running, bundle_path=bundle_path,
                     error_summary=sanitize_runtime_audit_text(exc), publish_failure=True)

    if not bundle.coverage.langfuse_source_available:
        return _fail(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            error_summary="Langfuse evidence source was unavailable; the day cannot be treated as empty.",
            publish_failure=True,
        )

    if not bundle.traces and not bundle.local_manifests and not bundle.local_fallbacks:
        previous_registry = store.read_issue_registry()
        markdown = render_empty_daily_report(
            window.audit_date,
            langfuse_available=bundle.coverage.langfuse_source_available,
            local_available=bundle.coverage.local_source_available,
            issues=report_issue_view(
                previous_registry,
                previous_registry,
                [],
                audit_date=window.audit_date,
            ),
        )
        return _commit_success(store=store, attempt=running, bundle_path=bundle_path,
                               markdown=markdown, registry=None,
                               commit_continuous_state=commit_continuous_state)

    if not requires_codex_audit(bundle):
        previous_registry = store.read_issue_registry()
        markdown = render_no_anomaly_daily_report(
            window.audit_date,
            trace_count=bundle.coverage.langfuse_trace_count,
            issues=report_issue_view(
                previous_registry,
                previous_registry,
                [],
                audit_date=window.audit_date,
            ),
        )
        return _commit_success(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            markdown=markdown,
            registry=None,
            commit_continuous_state=commit_continuous_state,
        )

    try:
        previous_registry = store.read_issue_registry()
        report = codex_runner(
            audit_date=window.audit_date,
            bundle_path=codex_input_path,
            issues_path=store.issues_path,
            repo_root=repo_root,
            output_path=store.codex_json_path(attempt_id),
            schema_path=store.codex_schema_path(attempt_id),
        )
        if report.audit_date != window.audit_date:
            raise ValueError("Codex daily report audit_date does not match the requested day")
        report = _add_deterministic_limitations(report, bundle)
        _validate_current_bundle_evidence(report, bundle)
        report = _downgrade_unverifiable_legacy_transitions(
            report,
            previous_registry=previous_registry,
            repo_root=repo_root,
        )
        report = _discard_earlier_context_commits(
            report,
            bundle=bundle,
            repo_root=repo_root,
        )
        _validate_repository_code_evidence(
            report,
            bundle=bundle,
            repo_root=repo_root,
            previous_registry=previous_registry,
            historical_refresh=not commit_continuous_state,
        )
        _validate_sensitive_text_overlap(report, bundle)
        report_registry = (
            merge_issue_registry(previous_registry, report.issues, window.audit_date)
            if commit_continuous_state
            else build_refresh_issue_registry(
                previous_registry,
                report.issues,
                window.audit_date,
            )
        )
        issues = (
            report_issue_view(
                previous_registry,
                report_registry,
                report.issues,
                audit_date=window.audit_date,
            )
            if commit_continuous_state
            else list(report.issues)
        )
        registry = report_registry if commit_continuous_state else None
        markdown = render_daily_codex_report(
            report,
            issues=issues,
            traces=bundle.traces,
        )
    except Exception as exc:
        return _fail(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            error_summary=sanitize_runtime_audit_text(exc),
            publish_failure=True,
        )

    return _commit_success(store=store, attempt=running, bundle_path=bundle_path,
                           markdown=markdown, registry=registry,
                           commit_continuous_state=commit_continuous_state)


def run_pending_daily_audits(
    *,
    yesterday: date,
    source: LangfuseAuditSource,
    local_trace_path: Path,
    store: RuntimeAuditArtifactStore,
    repo_root: Path,
    codex_runner: Callable[..., DailyCodexAuditReport],
    collected_at: datetime,
    judge_grace: timedelta = timedelta(minutes=15),
    low_score_threshold: float = DEFAULT_LOW_SCORE_THRESHOLD,
    local_ledger_retention_days: int = DEFAULT_LOCAL_LEDGER_RETENTION_DAYS,
) -> list[DailyAuditRunResult]:
    """Audit yesterday once; older failed or missed dates require explicit reruns."""

    results: list[DailyAuditRunResult] = []
    with store.daily_claim():
        _recover_pending_commits(store)
        dates = pending_audit_dates(
            yesterday=yesterday, last_completed=store.last_completed_date()
        )
        for audit_date in dates:
            try:
                result = run_one_daily_audit(
                    window=window_for_date(audit_date),
                    source=source,
                    local_trace_path=local_trace_path,
                    store=store,
                    repo_root=repo_root,
                    codex_runner=codex_runner,
                    collected_at=collected_at,
                    judge_grace=judge_grace,
                    low_score_threshold=low_score_threshold,
                    local_ledger_retention_days=local_ledger_retention_days,
                    commit_continuous_state=True,
                    _claimed=True,
                )
            except Exception as exc:
                raise DailyAuditDayError(
                    audit_date=audit_date,
                    completed_results=results,
                ) from exc
            results.append(result)
            if result.status == "failed":
                break
    return results


def recover_pending_daily_commits(store: RuntimeAuditArtifactStore) -> None:
    """Recover journaled daily commits before any source is created or consulted."""

    with store.daily_claim():
        _recover_pending_commits(store)


def run_failed_daily_audit(
    *, window: DailyAuditWindow, store: RuntimeAuditArtifactStore,
    collected_at: datetime, error_summary: str,
) -> DailyAuditRunResult:
    """Record an operator-visible failure when a source cannot even be constructed."""

    with store.daily_claim():
        _recover_pending_commits(store)
        attempt_id = store.allocate_audit_run_id(collected_at)
        bundle_path = store.staged_daily_bundle_path(attempt_id)
        attempt = DailyAuditAttempt(
            attempt_id=attempt_id, audit_date=window.audit_date, status="running",
            bundle_path=str(bundle_path), codex_output_path=str(store.codex_json_path(attempt_id)),
        )
        return _fail(store=store, attempt=attempt, bundle_path=bundle_path,
                     error_summary=error_summary, publish_failure=True)


def run_failed_pending_daily_audit(
    *, yesterday: date, store: RuntimeAuditArtifactStore,
    collected_at: datetime, error_summary: str,
) -> DailyAuditRunResult | None:
    """Claim the latest first pending day after automatic source setup fails."""

    with store.daily_claim():
        _recover_pending_commits(store)
        dates = pending_audit_dates(
            yesterday=yesterday, last_completed=store.last_completed_date()
        )
        if not dates:
            return None
        audit_date = dates[0]
        attempt_id = store.allocate_audit_run_id(collected_at)
        bundle_path = store.staged_daily_bundle_path(attempt_id)
        attempt = DailyAuditAttempt(
            attempt_id=attempt_id,
            audit_date=audit_date,
            status="running",
            bundle_path=str(bundle_path),
            codex_output_path=str(store.codex_json_path(attempt_id)),
        )
        return _fail(
            store=store,
            attempt=attempt,
            bundle_path=bundle_path,
            error_summary=error_summary,
            publish_failure=True,
        )


def _commit_success(
    *,
    store: RuntimeAuditArtifactStore,
    attempt: DailyAuditAttempt,
    bundle_path: Path,
    markdown: str,
    registry,
    commit_continuous_state: bool,
) -> DailyAuditRunResult:
    try:
        intent_path = store.write_commit_intent(
            attempt, markdown=markdown, registry=registry,
            commit_continuous_state=commit_continuous_state,
        )
    except ValueError as exc:
        return _fail(
            store=store, attempt=attempt, bundle_path=bundle_path,
            error_summary=sanitize_runtime_audit_text(exc), publish_failure=True,
        )
    try:
        intent = DailyAuditCommitIntent.model_validate_json(
            intent_path.read_text(encoding="utf-8")
        )
        report_path = _apply_commit_intent(store, intent)
    except (DailyCommitIntentRejected, ValidationError) as exc:
        store.quarantine_commit_intent(intent_path)
        return _fail(
            store=store, attempt=attempt, bundle_path=bundle_path,
            error_summary=sanitize_runtime_audit_text(exc), publish_failure=True,
        )
    return DailyAuditRunResult(audit_date=attempt.audit_date, status="succeeded",
                               attempt_id=attempt.attempt_id,
                               bundle_path=store.daily_bundle_path(attempt.audit_date),
                               report_path=report_path)


def _recover_pending_commits(store: RuntimeAuditArtifactStore) -> None:
    for path in store.commit_intent_files():
        try:
            intent = DailyAuditCommitIntent.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            _apply_commit_intent(store, intent)
        except (DailyCommitIntentRejected, ValidationError):
            store.quarantine_commit_intent(path)


def _apply_commit_intent(
    store: RuntimeAuditArtifactStore, intent: DailyAuditCommitIntent
) -> Path:
    attempt = intent.attempt
    if not intent.commit_continuous_state:
        bundle_path, codex_input_path = store.publish_daily_evidence(attempt)
        succeeded = _succeeded_attempt(
            attempt,
            bundle_path=bundle_path,
            codex_input_path=codex_input_path,
        )
        report_path = store.write_daily_report(
            attempt.audit_date, intent.markdown, replace=True
        )
        store.write_attempt(succeeded)
        store.clear_staged_daily_evidence(attempt)
        store.clear_commit_intent(attempt.attempt_id)
        return report_path
    current_watermark = store.read_daily_watermark()
    current_date = current_watermark.last_completed_date if current_watermark else None
    if current_date is not None and current_date > attempt.audit_date:
        raise DailyCommitIntentRejected(
            "stale daily commit intent cannot overwrite newer watermark"
        )
    expected_date = intent.expected_predecessor_watermark
    if expected_date is not None and attempt.audit_date <= expected_date:
        raise DailyCommitIntentRejected(
            "daily commit intent target is not newer than predecessor"
        )
    if current_date not in {expected_date, attempt.audit_date}:
        raise DailyCommitIntentRejected(
            "daily commit intent watermark precondition failed"
        )
    if current_date == attempt.audit_date and current_watermark.last_attempt_id != attempt.attempt_id:
        raise DailyCommitIntentRejected(
            "daily commit intent conflicts with completed attempt"
        )
    desired = intent.desired_registry_digest
    previous = intent.previous_registry_digest
    current_digest = store.issue_registry_digest()
    expected_digests = {previous} if desired is None else {previous, desired}
    if current_digest not in expected_digests:
        raise DailyCommitIntentRejected(
            "daily commit intent registry precondition failed"
        )
    registry = intent.registry
    if registry is not None and storage_registry_digest(registry) != desired:
        raise DailyCommitIntentRejected(
            "daily commit intent desired registry digest is invalid"
        )
    bundle_path, codex_input_path = store.publish_daily_evidence(attempt)
    succeeded = _succeeded_attempt(
        attempt,
        bundle_path=bundle_path,
        codex_input_path=codex_input_path,
    )
    report_path = store.write_daily_report(attempt.audit_date, intent.markdown, replace=True)
    if registry is not None and current_digest != desired:
        store.write_issue_registry(IssueRegistry.model_validate(registry.model_dump()))
    store.write_attempt(succeeded)
    if current_date != attempt.audit_date:
        store.mark_day_completed(attempt.audit_date, attempt_id=attempt.attempt_id,
                                 bundle_path=str(bundle_path))
    store.clear_staged_daily_evidence(attempt)
    store.clear_commit_intent(attempt.attempt_id)
    return report_path


def _succeeded_attempt(
    attempt: DailyAuditAttempt,
    *,
    bundle_path: Path,
    codex_input_path: Path,
) -> DailyAuditAttempt:
    return attempt.model_copy(
        update={
            "status": "succeeded",
            "error_summary": None,
            "bundle_path": str(bundle_path),
            "codex_input_path": str(codex_input_path),
        }
    )


def _fail(
    *,
    store: RuntimeAuditArtifactStore,
    attempt: DailyAuditAttempt,
    bundle_path: Path,
    error_summary: str,
    publish_failure: bool,
) -> DailyAuditRunResult:
    error_summary = sanitize_runtime_audit_text(error_summary)
    report_path = store.daily_report_path(attempt.audit_date)
    if publish_failure and not store.is_day_completed(attempt.audit_date):
        store.write_failed_daily_report_if_absent(
            attempt.audit_date,
            render_failed_daily_report(attempt.audit_date, error_summary),
        )
    failed = attempt.model_copy(update={"status": "failed", "error_summary": error_summary})
    store.write_attempt(failed)
    return DailyAuditRunResult(
        audit_date=attempt.audit_date,
        status="failed",
        attempt_id=attempt.attempt_id,
        bundle_path=bundle_path,
        report_path=report_path if report_path.exists() else None,
        error_summary=error_summary,
    )


def _validate_current_bundle_evidence(
    report: DailyCodexAuditReport, bundle: RuntimeAuditBundle,
) -> None:
    """Reject Codex lifecycle evidence not represented by this exact read-only bundle."""

    trace_ids = {trace.trace_id for trace in bundle.traces}
    trace_ids.update(manifest.trace_id for manifest in bundle.local_manifests)
    trace_ids.update(fallback.trace_id for fallback in bundle.local_fallbacks)
    observations = {
        (trace.trace_id, observation.observation_id)
        for trace in bundle.traces
        for observation in trace.observations
    }
    scores = {
        (trace.trace_id, score.score_id)
        for trace in bundle.traces
        for score in trace.scores
    }
    for issue in report.issues:
        for ref in [*issue.trace_evidence_refs, *issue.runtime_verification_refs]:
            trace_id, evidence_kind, evidence_id = _parse_trace_ref(ref)
            if trace_id not in trace_ids:
                raise ValueError("daily issue evidence ref is not in the current audit bundle")
            if (
                evidence_kind == "observation"
                and (trace_id, evidence_id) not in observations
            ):
                raise ValueError("daily issue observation ref is not in the current audit bundle")
            if evidence_kind == "score" and (trace_id, evidence_id) not in scores:
                raise ValueError("daily issue Score ref is not in the current audit bundle")


def _parse_trace_ref(ref: str) -> tuple[str, str | None, str | None]:
    value = ref.removeprefix("trace:")
    trace_id = value
    evidence_kind = None
    evidence_id = None
    for suffix_kind in ("observation", "score"):
        separator = f"/{suffix_kind}:"
        if separator in value:
            trace_id, evidence_id = value.split(separator, maxsplit=1)
            evidence_kind = suffix_kind
            break
    if (
        not trace_id
        or (evidence_kind is not None and not evidence_id)
        or "/observation:" in (evidence_id or "")
        or "/score:" in (evidence_id or "")
    ):
        raise ValueError("daily issue evidence ref is malformed")
    return trace_id, evidence_kind, evidence_id


_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SENSITIVE_OVERLAP_CHARS = 80


def _discard_earlier_context_commits(
    report: DailyCodexAuditReport,
    *,
    bundle: RuntimeAuditBundle,
    repo_root: Path,
) -> DailyCodexAuditReport:
    """Remove real commits that precede the latest bad trace from mixed evidence."""

    trace_times = _bundle_trace_times(bundle)
    normalized_issues = []
    for issue in report.issues:
        if issue.status != "code_addressed":
            normalized_issues.append(issue)
            continue
        bad_trace_times = [
            trace_times[_parse_trace_ref(ref)[0]]
            for ref in issue.trace_evidence_refs
            if _parse_trace_ref(ref)[0] in trace_times
        ]
        if not bad_trace_times:
            normalized_issues.append(issue)
            continue
        latest_bad_trace = max(bad_trace_times)
        filtered_refs = []
        for ref in issue.code_evidence_refs:
            if not ref.startswith("code:"):
                filtered_refs.append(ref)
                continue
            try:
                committed_at = _git_commit_time(repo_root, ref.removeprefix("code:"))
            except (OSError, subprocess.SubprocessError, ValueError):
                filtered_refs.append(ref)
                continue
            if committed_at > latest_bad_trace:
                filtered_refs.append(ref)
        normalized_issues.append(
            issue.model_copy(update={"code_evidence_refs": filtered_refs})
        )
    return report.model_copy(update={"issues": normalized_issues})


def _validate_repository_code_evidence(
    report: DailyCodexAuditReport,
    *,
    bundle: RuntimeAuditBundle,
    repo_root: Path,
    previous_registry: IssueRegistry,
    historical_refresh: bool,
) -> None:
    """Authenticate code-addressed refs against local Git and repository files."""

    repo_root = Path(repo_root).resolve()
    trace_times = _bundle_trace_times(bundle)
    for issue in report.issues:
        previous_issue = previous_registry.issues.get(issue.issue_key)
        if issue.status in {"runtime_verified", "regressed"} and previous_issue is not None:
            commit_times = _authenticated_commit_times(
                repo_root,
                previous_issue.code_evidence_refs,
            )
            verification_refs = (
                issue.runtime_verification_refs
                if issue.status == "runtime_verified"
                else issue.trace_evidence_refs
            )
            verification_times = [
                trace_times[_parse_trace_ref(ref)[0]]
                for ref in verification_refs
                if _parse_trace_ref(ref)[0] in trace_times
            ]
            if commit_times and (
                not verification_times or min(verification_times) <= max(commit_times)
            ):
                raise ValueError(
                    "runtime verification evidence must follow the recorded code fix"
                )
        if issue.status != "code_addressed":
            continue
        commit_refs = [
            ref.removeprefix("code:")
            for ref in issue.code_evidence_refs
            if ref.startswith("code:")
        ]
        if not commit_refs:
            raise ValueError("code_addressed requires a code:<commit-sha> reference")
        bad_trace_times = [
            trace_times[_parse_trace_ref(ref)[0]]
            for ref in issue.trace_evidence_refs
            if _parse_trace_ref(ref)[0] in trace_times
        ]
        for commit_sha in commit_refs:
            committed_at = _git_commit_time(repo_root, commit_sha)
            if bad_trace_times and committed_at <= max(bad_trace_times):
                raise ValueError(
                    "code evidence commit must follow the referenced bad trace"
                )
            if historical_refresh and committed_at >= bundle.window_end:
                raise ValueError("historical refresh code evidence postdates the audit window")
        for ref in issue.code_evidence_refs:
            if not ref.startswith("test:"):
                continue
            relative = PurePosixPath(ref.removeprefix("test:"))
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.parts[0] != "tests"
                or ".." in relative.parts
            ):
                raise ValueError("test evidence must be a repository-relative tests path")
            test_path = (repo_root / Path(*relative.parts)).resolve()
            if not test_path.is_relative_to(repo_root) or not test_path.is_file():
                raise ValueError("test evidence path does not exist in the repository")


def _downgrade_unverifiable_legacy_transitions(
    report: DailyCodexAuditReport,
    *,
    previous_registry: IssueRegistry,
    repo_root: Path,
) -> DailyCodexAuditReport:
    """Keep old registry refs readable without authenticating a terminal transition."""

    issues = []
    limitations = list(report.limitations)
    for issue in report.issues:
        previous = previous_registry.issues.get(issue.issue_key)
        if issue.status not in {"runtime_verified", "regressed"} or previous is None:
            issues.append(issue)
            continue
        if _authenticated_commit_times(repo_root, previous.code_evidence_refs):
            issues.append(issue)
            continue
        limitation = "旧版修复证据无法验证，因此暂时不能确认问题已经恢复。"
        limitations.append(limitation)
        issues.append(
            issue.model_copy(
                update={
                    "status": "uncertain",
                    "validation": "补录真实 Git commit SHA 后再等待自然运行验证。",
                    "runtime_verification_refs": [],
                }
            )
        )
    return report.model_copy(
        update={
            "issues": issues,
            "limitations": list(dict.fromkeys(limitations)),
        }
    )


def _git_commit_time(repo_root: Path, commit_sha: str) -> datetime:
    if not _COMMIT_SHA.fullmatch(commit_sha):
        raise ValueError("code evidence must use a hexadecimal Git commit SHA")
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "show",
            "-s",
            "--format=%H%n%cI",
            commit_sha,
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2 or len(lines[0]) != 40:
        raise ValueError("code evidence does not resolve to a repository commit")
    return datetime.fromisoformat(lines[1]).astimezone(timezone.utc)


def _authenticated_commit_times(repo_root: Path, refs: list[str]) -> list[datetime]:
    """Resolve valid historical commit refs while tolerating legacy ref formats."""

    commit_times: list[datetime] = []
    for ref in refs:
        if not ref.startswith("code:"):
            continue
        commit_sha = ref.removeprefix("code:")
        if not _COMMIT_SHA.fullmatch(commit_sha):
            continue
        try:
            commit_times.append(_git_commit_time(repo_root, commit_sha))
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return commit_times


def _bundle_trace_times(bundle: RuntimeAuditBundle) -> dict[str, datetime]:
    times = {
        trace.trace_id: trace.timestamp.astimezone(timezone.utc)
        for trace in bundle.traces
    }
    for manifest in bundle.local_manifests:
        times.setdefault(
            manifest.trace_id,
            manifest.first_event_at.astimezone(timezone.utc),
        )
    for fallback in bundle.local_fallbacks:
        if not fallback.timeline:
            continue
        first_event_at = min(event.created_at for event in fallback.timeline).astimezone(
            timezone.utc
        )
        existing = times.get(fallback.trace_id)
        if existing is None or first_event_at < existing:
            times[fallback.trace_id] = first_event_at
    return times


def _add_deterministic_limitations(
    report: DailyCodexAuditReport,
    bundle: RuntimeAuditBundle,
) -> DailyCodexAuditReport:
    if bundle.coverage.local_source_available:
        return report
    limitation = "本地完整性证据不可用，远端审计仍已完成，但无法执行本地完整性对账。"
    limitations = list(report.limitations)
    if limitation not in limitations:
        if len(limitations) >= 30:
            limitations[-1] = limitation
        else:
            limitations.append(limitation)
    return report.model_copy(update={"limitations": limitations})


def _validate_sensitive_text_overlap(
    report: DailyCodexAuditReport,
    bundle: RuntimeAuditBundle,
) -> None:
    report_texts = list(_report_human_text(report))
    sensitive_texts: list[str] = []
    for trace in bundle.traces:
        for value in (trace.input, trace.output, trace.metadata):
            sensitive_texts.extend(_string_leaves(value))
        for observation in trace.observations:
            for value in (observation.input, observation.output, observation.metadata):
                sensitive_texts.extend(_string_leaves(value))
    for report_text in report_texts:
        normalized_report = _normalize_overlap_text(report_text)
        if len(normalized_report) < _SENSITIVE_OVERLAP_CHARS:
            continue
        for sensitive_text in sensitive_texts:
            normalized_sensitive = _normalize_overlap_text(sensitive_text)
            if len(normalized_sensitive) < _SENSITIVE_OVERLAP_CHARS:
                continue
            if any(
                normalized_sensitive[index : index + _SENSITIVE_OVERLAP_CHARS]
                in normalized_report
                for index in range(
                    len(normalized_sensitive) - _SENSITIVE_OVERLAP_CHARS + 1
                )
            ):
                raise ValueError(
                    "Codex daily report copied a sensitive long source-text fragment"
                )


def _report_human_text(report: DailyCodexAuditReport) -> Iterator[str]:
    yield report.daily_summary
    yield report.activity_summary
    yield report.memory_summary
    yield report.infrastructure_summary
    yield from report.limitations
    for issue in report.issues:
        yield issue.title
        yield issue.plain_summary
        yield issue.user_impact
        yield issue.suggested_change
        yield issue.validation


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_string_leaves(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_string_leaves(child))
        return result
    return []


def _normalize_overlap_text(value: str) -> str:
    return " ".join(value.split())
