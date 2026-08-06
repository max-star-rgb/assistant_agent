"""Operator CLI for Langfuse-first runtime audit collection and reporting."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from assistant_agent.observability.runtime_audit.collector import collect_runtime_audit
from assistant_agent.observability.runtime_audit.daily_runner import (
    recover_pending_daily_commits,
    run_failed_daily_audit,
    run_failed_pending_daily_audit,
    run_one_daily_audit,
    run_pending_daily_audits,
)
from assistant_agent.observability.runtime_audit.daily_window import (
    pending_audit_dates,
    previous_day_window,
    window_for_date,
)
from assistant_agent.observability.runtime_audit.langfuse_source import (
    create_langfuse_audit_source_from_env,
)
from assistant_agent.observability.runtime_audit.models import RuntimeAuditBundle
from assistant_agent.observability.runtime_audit.online_evaluators import (
    configure_native_online_evaluators,
)
from assistant_agent.observability.runtime_audit.report import (
    render_codex_report,
    render_deterministic_report,
)
from assistant_agent.observability.runtime_audit.runner import (
    run_codex_report,
    run_daily_codex_report,
)
from assistant_agent.observability.runtime_audit.storage import RuntimeAuditArtifactStore
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.runtime.assistant_run_service import load_env_file


DEFAULT_ARTIFACT_ROOT = Path(".data/runtime_audit")
DEFAULT_LOCAL_TRACE_PATH = Path(".data/graph_trace.jsonl")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    store = RuntimeAuditArtifactStore(repo_root / args.artifact_root)
    if args.command == "run" and args.window_hours is None and args.dry_run:
        return _daily_dry_run(args, store=store)
    if args.command in {"collect", "run"} and args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "command": args.command,
                    "repo_root": str(repo_root),
                    "artifact_root": str(store.root),
                    "local_trace_path": str(repo_root / args.local_trace_path),
                    "window_hours": args.window_hours,
                    "codex_enabled": args.command == "run" and not args.skip_codex,
                    "production_mutation_allowed": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command in {"collect", "run", "configure-evaluators"} and not args.no_env_file:
        load_env_file(repo_root / args.env_file, override=False)
    try:
        if args.command == "configure-evaluators":
            if args.apply and not args.allow_online_judge:
                raise RuntimeError("--apply also requires --allow-online-judge.")
            source = create_langfuse_audit_source_from_env(os.environ)
            try:
                result = configure_native_online_evaluators(
                    source.client,
                    apply=args.apply,
                    model_provider=args.model_provider,
                    model=args.model,
                )
            finally:
                source.close()
            print(result.model_dump_json())
            return 0
        if args.command == "collect":
            bundle_path, _, _ = _collect(args, repo_root=repo_root, store=store)
            print(json.dumps({"status": "collected", "bundle_path": str(bundle_path)}))
            return 0
        if args.command == "run" and args.window_hours is None and not args.skip_codex:
            return _run_daily(args, repo_root=repo_root, store=store)
        if args.command == "report":
            bundle_path = _resolve_bundle_path(args.bundle, store=store, repo_root=repo_root)
            return _report(
                bundle_path,
                repo_root=repo_root,
                store=store,
                skip_codex=args.skip_codex,
                codex_timeout_seconds=args.codex_timeout_seconds,
            )
        bundle_path, report_path, bundle = _collect(args, repo_root=repo_root, store=store)
        if args.skip_codex:
            print(
                json.dumps(
                    {
                        "status": "completed_without_codex",
                        "audit_run_id": bundle.audit_run_id,
                        "bundle_path": str(bundle_path),
                        "report_path": str(report_path),
                    }
                )
            )
            return 0
        return _report(
            bundle_path,
            repo_root=repo_root,
            store=store,
            skip_codex=False,
            codex_timeout_seconds=args.codex_timeout_seconds,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": sanitize_error_message(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return 2


def _daily_dry_run(args, *, store: RuntimeAuditArtifactStore) -> int:
    if args.date is not None:
        dates = [args.date]
    else:
        yesterday = previous_day_window(datetime.now(timezone.utc)).audit_date
        from assistant_agent.observability.runtime_audit.daily_window import pending_audit_dates

        dates = pending_audit_dates(
            yesterday=yesterday, last_completed=store.last_completed_date()
        )
    print(
        json.dumps(
            {
                "status": "dry_run",
                "audit_dates": [item.isoformat() for item in dates],
                "report_paths": [str(store.daily_report_path(item)) for item in dates],
                "failed_date": None,
                "production_mutation_allowed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run_daily(args, *, repo_root: Path, store: RuntimeAuditArtifactStore) -> int:
    """Run explicit refreshes or ordered backfill through the resumable daily loop."""

    collected_at = datetime.now(timezone.utc)
    recover_pending_daily_commits(store)
    yesterday = previous_day_window(collected_at).audit_date
    dates = [args.date] if args.date is not None else pending_audit_dates(
        yesterday=yesterday, last_completed=store.last_completed_date()
    )
    if not dates:
        print(json.dumps({"audit_dates": [], "report_paths": [], "failed_date": None}))
        return 0
    source = None
    try:
        source = create_langfuse_audit_source_from_env(os.environ)
        def runner(**kwargs):
            return run_daily_codex_report(
                **kwargs, timeout_seconds=args.codex_timeout_seconds
            )
        common = {
            "source": source,
            "local_trace_path": repo_root / args.local_trace_path,
            "store": store,
            "repo_root": repo_root,
            "codex_runner": runner,
            "collected_at": collected_at,
            "judge_grace": timedelta(minutes=args.judge_grace_minutes),
            "low_score_threshold": args.low_score_threshold,
        }
        if args.date is not None:
            results = [
                run_one_daily_audit(
                    window=window_for_date(args.date),
                    commit_continuous_state=False,
                    **common,
                )
            ]
        else:
            results = run_pending_daily_audits(yesterday=yesterday, **common)
    except Exception as exc:
        if args.date is not None:
            results = [
                run_failed_daily_audit(
                    window=window_for_date(args.date),
                    store=store,
                    collected_at=collected_at,
                    error_summary=sanitize_error_message(exc),
                )
            ]
        else:
            failed_result = run_failed_pending_daily_audit(
                yesterday=yesterday,
                store=store,
                collected_at=collected_at,
                error_summary=sanitize_error_message(exc),
            )
            results = [] if failed_result is None else [failed_result]
    finally:
        if source is not None:
            try:
                source.close()
            except Exception as exc:
                print(
                    json.dumps({"status": "source_close_warning", "message": sanitize_error_message(exc)}),
                    file=sys.stderr,
                )
    failed = next((item for item in results if item.status == "failed"), None)
    print(
        json.dumps(
            {
                "audit_dates": [item.audit_date.isoformat() for item in results],
                "report_paths": [str(item.report_path) for item in results if item.report_path],
                "failed_date": failed.audit_date.isoformat() if failed else None,
            },
            ensure_ascii=False,
        )
    )
    return 2 if failed else 0


def _collect(args, *, repo_root: Path, store: RuntimeAuditArtifactStore):
    with store.daily_claim():
        return _collect_locked(args, repo_root=repo_root, store=store)


def _collect_locked(args, *, repo_root: Path, store: RuntimeAuditArtifactStore):
    collected_at = datetime.now(timezone.utc)
    if args.window_hours is not None:
        window_end = collected_at
        window_start = window_end - timedelta(hours=args.window_hours)
    else:
        window = window_for_date(args.date) if args.date else previous_day_window(collected_at)
        window_start = window.start_utc
        window_end = window.end_utc
    audit_run_id = store.allocate_audit_run_id(collected_at)
    source = create_langfuse_audit_source_from_env(os.environ)
    try:
        bundle = collect_runtime_audit(
            source=source,
            local_trace_path=repo_root / args.local_trace_path,
            window_start=window_start,
            window_end=window_end,
            collected_at=collected_at,
            audit_run_id=audit_run_id,
            judge_grace=timedelta(minutes=args.judge_grace_minutes),
            low_score_threshold=args.low_score_threshold,
        )
    finally:
        source.close()
    bundle_path = store.write_bundle(bundle)
    report_path = store.write_deterministic_report(bundle, render_deterministic_report(bundle))
    return bundle_path, report_path, bundle


def _report(
    bundle_path: Path,
    *,
    repo_root: Path,
    store: RuntimeAuditArtifactStore,
    skip_codex: bool,
    codex_timeout_seconds: float,
) -> int:
    bundle = RuntimeAuditBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    if skip_codex:
        path = store.write_deterministic_report(bundle, render_deterministic_report(bundle))
        print(json.dumps({"status": "deterministic_report", "report_path": str(path)}))
        return 0
    report = run_codex_report(
        bundle_path=bundle_path,
        repo_root=repo_root,
        output_path=store.codex_json_path(bundle.audit_run_id),
        schema_path=store.codex_schema_path(bundle.audit_run_id),
        timeout_seconds=codex_timeout_seconds,
    )
    markdown_path = store.write_deterministic_report(bundle, render_codex_report(report))
    print(
        json.dumps(
            {
                "status": "codex_report",
                "audit_run_id": bundle.audit_run_id,
                "json_path": str(store.codex_json_path(bundle.audit_run_id)),
                "markdown_path": str(markdown_path),
            }
        )
    )
    return 0


def _resolve_bundle_path(value: str | None, *, store: RuntimeAuditArtifactStore, repo_root: Path) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else (repo_root / path).resolve()
    artifact_path = (
        store.latest_bundle_path
        if store.latest_bundle_path.exists()
        else store.watermark_path
    )
    if not artifact_path.exists():
        raise RuntimeError("No runtime audit watermark exists; run collect first.")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    path = Path(payload["bundle_path"])
    return path if path.is_absolute() else (repo_root / path).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-env-file", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "run"):
        child = subparsers.add_parser(name)
        window = child.add_mutually_exclusive_group()
        window.add_argument("--date", type=date.fromisoformat)
        window.add_argument("--window-hours", type=float)
        child.add_argument("--judge-grace-minutes", type=float, default=15.0)
        child.add_argument("--low-score-threshold", type=float, default=0.5)
        child.add_argument("--local-trace-path", default=str(DEFAULT_LOCAL_TRACE_PATH))
        child.add_argument("--dry-run", action="store_true")
        if name == "run":
            child.add_argument("--skip-codex", action="store_true")
            child.add_argument("--codex-timeout-seconds", type=float, default=900.0)
    report = subparsers.add_parser("report")
    report.add_argument("--bundle")
    report.add_argument("--skip-codex", action="store_true")
    report.add_argument("--codex-timeout-seconds", type=float, default=900.0)
    evaluators = subparsers.add_parser("configure-evaluators")
    evaluators.add_argument("--model-provider", default="deepseek-judge")
    evaluators.add_argument("--model", default="deepseek-v4-flash")
    evaluators.add_argument("--apply", action="store_true")
    evaluators.add_argument("--allow-online-judge", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
