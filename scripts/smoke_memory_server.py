#!/usr/bin/env python3
# ruff: noqa: E402
"""Manual smoke entry point for external Memory Server retrieval."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.memory.remote import MemoryServerMediaFile, MemoryServerTaskStatusResult, RemoteMemoryClient
from assistant_agent.schemas.memory import MemoryQuery
from assistant_agent.services.provider_errors import sanitize_error_message


_TERMINAL_MEDIA_TASK_STATUSES = {"completed", "failed"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an opt-in Memory Server health and query smoke test.",
    )
    parser.add_argument("--base-url", default=None, help="Memory Server base URL, e.g. http://127.0.0.1:5200.")
    parser.add_argument("--user-id", required=True, help="User id to use for the scoped health and query calls.")
    parser.add_argument("--session-id", default=None, help="Optional session id for health and query calls.")
    parser.add_argument("--query", default=None, help="Natural language memory query.")
    parser.add_argument("--top-k", type=int, default=3, help="Maximum text memories to retrieve.")
    parser.add_argument("--timeout-seconds", type=float, default=2.0, help="HTTP timeout for each Memory Server call.")
    parser.add_argument("--strategy", default="vector", help="Memory Server retrieval strategy.")
    parser.add_argument("--trace", action="store_true", help="Request Memory Server trace metadata.")
    parser.add_argument("--health-only", action="store_true", help="Only call /v1/health and skip query/media smoke.")
    parser.add_argument("--media-file-url", default=None, help="Optional media URL/path to upload after query smoke.")
    parser.add_argument("--media-filename", default=None, help="Original filename for the optional media upload smoke.")
    parser.add_argument("--media-type", default=None, help="Media type for the optional upload smoke, e.g. video.")
    parser.add_argument("--media-start-time", default=None, help="ISO timestamp for optional media upload start_time.")
    parser.add_argument("--media-file-id", default=None, help="Optional globally unique file_id for media upload smoke.")
    parser.add_argument("--wait", action="store_true", help="Poll optional media ingestion until terminal status.")
    parser.add_argument(
        "--wait-timeout-seconds",
        type=float,
        default=30.0,
        help="Maximum seconds to wait for optional media ingestion when --wait is set.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
        help="Seconds between task status polls when --wait is set.",
    )
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = os.environ if env is None else env
    request_error = _validate_request_args(args)
    if request_error:
        _print_json(
            {
                "status": "invalid_request",
                "capability": "memory_server",
                "error": request_error,
            }
        )
        return 2
    media_error = _validate_media_args(args)
    if media_error:
        _print_json(
            {
                "status": "invalid_request",
                "capability": "memory_server",
                "error": media_error,
            }
        )
        return 2
    base_url = args.base_url or source.get("MEMORY_SERVER_BASE_URL")
    if not base_url:
        _print_json(
            {
                "status": "provider_unconfigured",
                "capability": "memory_server",
                "error": "missing MEMORY_SERVER_BASE_URL",
            }
        )
        return 2

    client = RemoteMemoryClient(
        base_url=base_url,
        timeout_seconds=args.timeout_seconds,
        query_strategy=args.strategy,
        direct_answer=False,
        include_media_chunks=False,
        trace=args.trace,
    )
    try:
        health = client.health(user_id=args.user_id, session_id=args.session_id)
    except Exception as exc:
        error = _smoke_error("memory_server_health_failed", "memory server health failed", detail=exc)
        _print_json(
            {
                "status": "failed",
                "capability": "memory_server",
                "base_url": base_url,
                "health_status": "failed",
                "user_id": args.user_id,
                "session_id": args.session_id,
                "diagnosis": _health_failure_diagnosis(error.get("detail", "")),
                "errors": [error],
            }
        )
        return 1
    if args.health_only:
        output = {
            "status": "success" if str(health.get("status") or "").lower() == "ok" else "failed",
            "capability": "memory_server",
            "base_url": base_url,
            "health_status": health.get("status", ""),
            "health_version": health.get("version", ""),
            "user_id": args.user_id,
            "session_id": args.session_id,
            "errors": [],
        }
        _print_json(output)
        return 0 if output["status"] == "success" else 1
    query = MemoryQuery(
        user_id=args.user_id,
        session_id=args.session_id,
        query=args.query,
        top_k=args.top_k,
    )
    result = client.query_memories(query)
    output = {
        "status": "success" if not result.errors else "failed",
        "capability": "memory_server",
        "base_url": base_url,
        "health_status": health.get("status", ""),
        "health_version": health.get("version", ""),
        "query": args.query,
        "user_id": args.user_id,
        "session_id": args.session_id,
        "strategy": args.strategy,
        "direct_answer": False,
        "include_media_chunks": False,
        "result_count": len(result.items),
        "memory_ids": [item.memory_id for item in result.items],
        "summaries": [item.summary for item in result.items],
        "errors": result.errors,
    }
    if args.media_file_url:
        file_id = args.media_file_id or _generated_media_file_id(
            user_id=args.user_id,
            session_id=args.session_id,
            filename=args.media_filename,
        )
        upload_result = client.upload_media(
            user_id=args.user_id,
            session_id=args.session_id or "default",
            files=[
                MemoryServerMediaFile(
                    file_id=file_id,
                    file_url=args.media_file_url,
                    filename=args.media_filename,
                    media_type=args.media_type,
                    start_time=_parse_media_start_time(args.media_start_time),
                )
            ],
        )
        output["media_upload"] = {
            "status": upload_result.status,
            "task_id": upload_result.task_id,
            "accepted_count": upload_result.accepted_count,
            "file_id": file_id,
            "errors": upload_result.errors,
        }
        if upload_result.task_id and not upload_result.errors:
            status_result, wait_summary = _poll_media_task_status(
                client,
                user_id=args.user_id,
                task_id=upload_result.task_id,
                wait=args.wait,
                timeout_seconds=args.wait_timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
            output["media_task_status"] = _task_status_payload(status_result)
            if wait_summary is not None:
                output["media_task_wait"] = wait_summary
    _print_json(output)
    return 0 if _smoke_succeeded(output) else 1


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _smoke_error(code: str, message: str, *, detail: object | None = None) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "recoverable": True,
    }
    if detail is not None:
        error["detail"] = sanitize_error_message(detail)
    return error


def _health_failure_diagnosis(detail: object) -> dict[str, str]:
    normalized = str(detail).lower()
    if "connection refused" in normalized or "errno 111" in normalized:
        return {
            "code": "memory_server_not_listening",
            "message": "Memory Server is not listening at the configured base URL.",
            "next_step": "Start the external Memory Server and rerun the health-only smoke check.",
        }
    if "timed out" in normalized or "timeout" in normalized:
        return {
            "code": "memory_server_health_timeout",
            "message": "Memory Server health check timed out.",
            "next_step": "Check service startup, Docker/network routing, and the health endpoint latency.",
        }
    return {
        "code": "memory_server_health_unavailable",
        "message": "Memory Server health check failed.",
        "next_step": "Inspect the sanitized error detail, then rerun the health-only smoke check.",
    }


def _validate_request_args(args: argparse.Namespace) -> str | None:
    if args.health_only and any(
        (
            args.media_file_url,
            args.media_filename,
            args.media_type,
            args.media_start_time,
            args.media_file_id,
            args.wait,
        )
    ):
        return "memory server --health-only cannot be combined with media smoke arguments"
    if not args.health_only and not args.query:
        return "memory server smoke requires --query unless --health-only is set"
    return None


def _validate_media_args(args: argparse.Namespace) -> str | None:
    media_values = [
        args.media_file_url,
        args.media_filename,
        args.media_type,
        args.media_start_time,
        args.media_file_id,
    ]
    if args.wait and not any(media_values):
        return "media smoke --wait requires media upload arguments"
    if args.wait_timeout_seconds < 0:
        return "media smoke --wait-timeout-seconds must be non-negative"
    if args.poll_interval_seconds <= 0:
        return "media smoke --poll-interval-seconds must be positive"
    if not any(media_values):
        return None
    if not all((args.media_file_url, args.media_filename, args.media_type, args.media_start_time)):
        return "media smoke requires --media-filename, --media-type, and --media-start-time"
    try:
        _parse_media_start_time(args.media_start_time)
    except ValueError:
        return "media smoke requires ISO --media-start-time"
    return None


def _parse_media_start_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _generated_media_file_id(*, user_id: str, session_id: str | None, filename: str) -> str:
    parts = [
        "smoke",
        _safe_id_part(user_id),
        _safe_id_part(session_id or "default"),
        _safe_id_part(Path(filename).stem or "media"),
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        uuid4().hex[:12],
    ]
    return "-".join(part for part in parts if part)


def _safe_id_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value).strip("-") or "unknown"


def _poll_media_task_status(
    client: RemoteMemoryClient,
    *,
    user_id: str,
    task_id: str,
    wait: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[MemoryServerTaskStatusResult, dict[str, object] | None]:
    started_at = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        result = client.task_status(user_id=user_id, task_id=task_id)
        terminal = _is_terminal_media_task_status(result.status)
        if not wait:
            return result, None
        if terminal or result.errors:
            return result, _media_task_wait_summary(
                attempts=attempts,
                terminal=terminal,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        elapsed = time.monotonic() - started_at
        if elapsed + 1e-9 >= timeout_seconds:
            return result, _media_task_wait_summary(
                attempts=attempts,
                terminal=False,
                timed_out=True,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        sleep_seconds = min(poll_interval_seconds, max(0.0, timeout_seconds - elapsed))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def _task_status_payload(result: MemoryServerTaskStatusResult) -> dict[str, object]:
    return {
        "task_id": result.task_id,
        "status": result.status,
        "total_files": result.total_files,
        "processed_files": result.processed_files,
        "failed_files": result.failed_files,
        "scope_warning": result.scope_warning,
        "errors": result.errors,
    }


def _media_task_wait_summary(
    *,
    attempts: int,
    terminal: bool,
    timed_out: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, object]:
    return {
        "enabled": True,
        "poll_attempts": attempts,
        "terminal": terminal,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
    }


def _is_terminal_media_task_status(status: str) -> bool:
    return status.strip().lower() in _TERMINAL_MEDIA_TASK_STATUSES


def _smoke_succeeded(output: Mapping[str, object]) -> bool:
    if output.get("status") != "success":
        return False
    media_upload = output.get("media_upload")
    if isinstance(media_upload, Mapping) and media_upload.get("errors"):
        return False
    media_task_status = output.get("media_task_status")
    if isinstance(media_task_status, Mapping) and media_task_status.get("errors"):
        return False
    if isinstance(media_task_status, Mapping) and str(media_task_status.get("status") or "").lower() in {
        "failed",
        "not_found",
    }:
        return False
    media_task_wait = output.get("media_task_wait")
    if isinstance(media_task_wait, Mapping) and media_task_wait.get("timed_out"):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
