"""Resumable, read-only orchestration for one Shanghai-calendar audit day."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from assistant_agent.observability.runtime_audit.collector import (
    DEFAULT_LOW_SCORE_THRESHOLD,
    LangfuseAuditSource,
    collect_runtime_audit,
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
from assistant_agent.observability.runtime_audit.issues import merge_issue_registry
from assistant_agent.observability.runtime_audit.report import (
    render_daily_codex_report,
    render_empty_daily_report,
    render_failed_daily_report,
)
from assistant_agent.observability.runtime_audit.storage import (
    RuntimeAuditArtifactStore,
    registry_digest as storage_registry_digest,
)
from assistant_agent.providers.provider_errors import sanitize_error_message


class DailyCommitIntentRejected(RuntimeError):
    """A journal cannot be safely applied to the current persisted state."""


class DailyAuditRunResult(BaseModel):
    audit_date: date
    status: Literal["succeeded", "failed"]
    attempt_id: str
    bundle_path: Path
    report_path: Path | None = None
    error_summary: str | None = None


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
    commit_continuous_state: bool = True,
    _claimed: bool = False,
) -> DailyAuditRunResult:
    """Collect, publish, and checkpoint one day without crossing a failed state."""

    claim = nullcontext() if _claimed else store.daily_claim()
    with claim:
        _recover_pending_commits(store)
        return _run_one_locked(
            window=window, source=source, local_trace_path=local_trace_path,
            store=store, repo_root=repo_root, codex_runner=codex_runner,
            collected_at=collected_at, judge_grace=judge_grace,
            low_score_threshold=low_score_threshold,
            commit_continuous_state=commit_continuous_state,
        )


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
        and window.audit_date != predecessor + timedelta(days=1)
    ):
        raise ValueError("daily commit target must follow its predecessor")
    attempt_id = store.allocate_audit_run_id(collected_at)
    bundle_path = store.inbox_dir / f"{attempt_id}.json"
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
        bundle_path = store.write_bundle(bundle)
        running = running.model_copy(update={"bundle_path": str(bundle_path)})
        store.write_attempt(running)
    except Exception as exc:
        return _fail(store=store, attempt=running, bundle_path=bundle_path,
                     error_summary=sanitize_error_message(exc), publish_failure=True)

    if not bundle.coverage.langfuse_source_available:
        return _fail(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            error_summary="Langfuse evidence source was unavailable; the day cannot be treated as empty.",
            publish_failure=True,
        )

    if not bundle.traces and not bundle.local_manifests and not bundle.local_fallbacks:
        markdown = render_empty_daily_report(
            window.audit_date,
            langfuse_available=bundle.coverage.langfuse_source_available,
            local_available=bundle.coverage.local_source_available,
        )
        if not bundle.coverage.local_source_available:
            return _fail(
                store=store,
                attempt=running,
                bundle_path=bundle_path,
                error_summary="Local completeness evidence source was unavailable; the day cannot be treated as empty.",
                publish_failure=True,
            )
        return _commit_success(store=store, attempt=running, bundle_path=bundle_path,
                               markdown=markdown, registry=None,
                               commit_continuous_state=commit_continuous_state)

    try:
        report = codex_runner(
            audit_date=window.audit_date,
            bundle_path=bundle_path,
            issues_path=store.issues_path,
            repo_root=repo_root,
            output_path=store.codex_json_path(attempt_id),
            schema_path=store.codex_schema_path(attempt_id),
        )
        if report.audit_date != window.audit_date:
            raise ValueError("Codex daily report audit_date does not match the requested day")
        _validate_current_bundle_evidence(report, bundle)
        registry = (
            merge_issue_registry(
                store.read_issue_registry(), report.issues, window.audit_date
            )
            if commit_continuous_state
            else None
        )
        markdown = render_daily_codex_report(report)
    except Exception as exc:
        return _fail(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            error_summary=sanitize_error_message(exc),
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
) -> list[DailyAuditRunResult]:
    """Backfill in calendar order, stopping at the first failed daily attempt."""

    results: list[DailyAuditRunResult] = []
    with store.daily_claim():
        _recover_pending_commits(store)
        dates = pending_audit_dates(
            yesterday=yesterday, last_completed=store.last_completed_date()
        )
        for audit_date in dates:
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
                commit_continuous_state=True,
                _claimed=True,
            )
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
        bundle_path = store.inbox_dir / f"{attempt_id}.json"
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
        bundle_path = store.inbox_dir / f"{attempt_id}.json"
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
            error_summary=sanitize_error_message(exc), publish_failure=True,
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
            error_summary=sanitize_error_message(exc), publish_failure=True,
        )
    return DailyAuditRunResult(audit_date=attempt.audit_date, status="succeeded",
                               attempt_id=attempt.attempt_id, bundle_path=bundle_path,
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
        report_path = store.write_daily_report(
            attempt.audit_date, intent.markdown, replace=True
        )
        store.write_attempt(
            attempt.model_copy(update={"status": "succeeded", "error_summary": None})
        )
        store.clear_commit_intent(attempt.attempt_id)
        return report_path
    current_watermark = store.read_daily_watermark()
    current_date = current_watermark.last_completed_date if current_watermark else None
    if current_date is not None and current_date > attempt.audit_date:
        raise DailyCommitIntentRejected(
            "stale daily commit intent cannot overwrite newer watermark"
        )
    expected_date = intent.expected_predecessor_watermark
    if expected_date is not None and attempt.audit_date != expected_date + timedelta(days=1):
        raise DailyCommitIntentRejected(
            "daily commit intent target is not adjacent to predecessor"
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
    report_path = store.write_daily_report(attempt.audit_date, intent.markdown, replace=True)
    if registry is not None and current_digest != desired:
        store.write_issue_registry(IssueRegistry.model_validate(registry.model_dump()))
    succeeded = attempt.model_copy(update={"status": "succeeded", "error_summary": None})
    store.write_attempt(succeeded)
    if current_date != attempt.audit_date:
        store.mark_day_completed(attempt.audit_date, attempt_id=attempt.attempt_id,
                                 bundle_path=attempt.bundle_path)
    store.clear_commit_intent(attempt.attempt_id)
    return report_path


def _fail(
    *,
    store: RuntimeAuditArtifactStore,
    attempt: DailyAuditAttempt,
    bundle_path: Path,
    error_summary: str,
    publish_failure: bool,
) -> DailyAuditRunResult:
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
    report: DailyCodexAuditReport, bundle: object,
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
    for issue in report.issues:
        for ref in [*issue.trace_evidence_refs, *issue.runtime_verification_refs]:
            trace_id, observation_id = _parse_trace_ref(ref)
            if trace_id not in trace_ids:
                raise ValueError("daily issue evidence ref is not in the current audit bundle")
            if observation_id is not None and (trace_id, observation_id) not in observations:
                raise ValueError("daily issue observation ref is not in the current audit bundle")


def _parse_trace_ref(ref: str) -> tuple[str, str | None]:
    value = ref.removeprefix("trace:")
    trace_id, separator, suffix = value.partition("/observation:")
    if not trace_id or (separator and not suffix):
        raise ValueError("daily issue evidence ref is malformed")
    return trace_id, suffix if separator else None
