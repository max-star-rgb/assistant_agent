"""Resumable, read-only orchestration for one Shanghai-calendar audit day."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from assistant_agent.observability.runtime_audit.collector import (
    DEFAULT_LOW_SCORE_THRESHOLD,
    LangfuseAuditSource,
    collect_runtime_audit,
)
from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditAttempt,
    DailyCodexAuditReport,
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
from assistant_agent.observability.runtime_audit.storage import RuntimeAuditArtifactStore
from assistant_agent.providers.provider_errors import sanitize_error_message


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
) -> DailyAuditRunResult:
    """Collect, publish, and checkpoint one day without crossing a failed state."""

    attempt_id = store.allocate_audit_run_id(collected_at)
    bundle = collect_runtime_audit(
        source=source,
        local_trace_path=local_trace_path,
        window_start=window.start_utc,
        window_end=window.end_utc,
        collected_at=collected_at,
        audit_run_id=attempt_id,
        judge_grace=judge_grace,
        low_score_threshold=low_score_threshold,
    )
    bundle_path = store.write_bundle(bundle)
    running = DailyAuditAttempt(
        attempt_id=attempt_id,
        audit_date=window.audit_date,
        status="running",
        bundle_path=str(bundle_path),
    )
    store.write_attempt(running)

    if not bundle.coverage.langfuse_source_available:
        return _fail(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            error_summary="Langfuse evidence source was unavailable; the day cannot be treated as empty.",
            publish_failure=True,
        )

    if not bundle.traces:
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
        return _succeed(
            store=store,
            attempt=running,
            bundle_path=bundle_path,
            markdown=markdown,
        )

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
        registry = merge_issue_registry(
            store.read_issue_registry(), report.issues, window.audit_date
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

    # A completed report is published before its related internal checkpoint.  Each
    # file is atomically replaced; a failure in a later step cannot advance watermark.
    report_path = store.write_daily_report(window.audit_date, markdown, replace=True)
    store.write_issue_registry(registry)
    succeeded = running.model_copy(update={"status": "succeeded"})
    store.write_attempt(succeeded)
    store.mark_day_completed(
        window.audit_date, attempt_id=attempt_id, bundle_path=str(bundle_path)
    )
    return DailyAuditRunResult(
        audit_date=window.audit_date,
        status="succeeded",
        attempt_id=attempt_id,
        bundle_path=bundle_path,
        report_path=report_path,
    )


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
    for audit_date in pending_audit_dates(
        yesterday=yesterday, last_completed=store.last_completed_date()
    ):
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
        )
        results.append(result)
        if result.status == "failed":
            break
    return results


def _succeed(
    *,
    store: RuntimeAuditArtifactStore,
    attempt: DailyAuditAttempt,
    bundle_path: Path,
    markdown: str,
) -> DailyAuditRunResult:
    report_path = store.write_daily_report(attempt.audit_date, markdown, replace=True)
    succeeded = attempt.model_copy(update={"status": "succeeded"})
    store.write_attempt(succeeded)
    store.mark_day_completed(
        attempt.audit_date, attempt_id=attempt.attempt_id, bundle_path=str(bundle_path)
    )
    return DailyAuditRunResult(
        audit_date=attempt.audit_date,
        status="succeeded",
        attempt_id=attempt.attempt_id,
        bundle_path=bundle_path,
        report_path=report_path,
    )


def _fail(
    *,
    store: RuntimeAuditArtifactStore,
    attempt: DailyAuditAttempt,
    bundle_path: Path,
    error_summary: str,
    publish_failure: bool,
) -> DailyAuditRunResult:
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
