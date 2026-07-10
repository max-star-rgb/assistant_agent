#!/usr/bin/env python3
# ruff: noqa: E402
"""Offline-first smoke entry point for dual-core memory acceptance."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.factory import create_memory_store
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.remote import (
    HybridMemoryStore,
    MemoryServerRequest,
    MemoryServiceOperationError,
    RemoteMemoryClient as _RemoteMemoryClient,
    RemoteServiceMemoryStore,
    UnavailableRemoteMemoryServiceAdapter,
)
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryQuery
from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


RemoteMemoryClient = _RemoteMemoryClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run offline-first acceptance checks for dual-core memory.",
    )
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="Run only local/offline checks and skip external Memory Server even if configured elsewhere.",
    )
    parser.add_argument(
        "--memory-server-base-url",
        default=None,
        help="Optional explicit Memory Server URL for health/query smoke.",
    )
    parser.add_argument("--user-id", default="dual_core_smoke_user", help="User id for the optional remote check.")
    parser.add_argument("--session-id", default="dual_core_smoke_session", help="Session id for smoke checks.")
    parser.add_argument("--query", default="上次早餐", help="Natural language query for the optional remote check.")
    parser.add_argument("--timeout-seconds", type=float, default=2.0, help="HTTP timeout for optional remote calls.")
    parser.add_argument("--strategy", default="vector", help="Memory Server retrieval strategy for optional remote calls.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _ = env
    payload = run_smoke(
        offline_only=bool(args.offline_only or not args.memory_server_base_url),
        memory_server_base_url=args.memory_server_base_url,
        user_id=args.user_id,
        session_id=args.session_id,
        query=args.query,
        timeout_seconds=args.timeout_seconds,
        strategy=args.strategy,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "succeeded" else 1


def run_smoke(
    *,
    offline_only: bool = True,
    memory_server_base_url: str | None = None,
    user_id: str = "dual_core_smoke_user",
    session_id: str = "dual_core_smoke_session",
    query: str = "上次早餐",
    timeout_seconds: float = 2.0,
    strategy: str = "vector",
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="assistant-agent-memory-dual-core-") as tmp:
        tmp_path = Path(tmp)
        checks["local_sqlite_core"] = _check_local_sqlite_core(tmp_path, user_id=user_id, session_id=session_id)
        checks["dual_core_degradation"] = _check_dual_core_degradation(user_id=user_id, session_id=session_id)
        checks["remote_service_lifecycle_failure"] = _check_remote_service_lifecycle_failure(
            user_id=user_id,
            session_id=session_id,
        )
        checks["memory_quality_eval"] = _check_memory_quality_eval()
        if not offline_only and memory_server_base_url:
            checks["external_memory_server"] = _check_external_memory_server(
                base_url=memory_server_base_url,
                user_id=user_id,
                session_id=session_id,
                query_text=query,
                timeout_seconds=timeout_seconds,
                strategy=strategy,
            )
    succeeded = all(check.get("passed") is True for check in checks.values())
    return {
        "status": "succeeded" if succeeded else "failed",
        "capability": "memory_dual_core",
        "offline_only": offline_only,
        "external_memory_server": "skipped" if offline_only else "checked",
        "checks": checks,
        "errors": _failed_check_codes(checks),
    }


def _check_local_sqlite_core(tmp_path: Path, *, user_id: str, session_id: str) -> dict[str, Any]:
    try:
        sqlite_path = tmp_path / "dual_core_local.sqlite3"
        store = create_memory_store(ProviderConfig(memory_backend="sqlite", memory_path=str(sqlite_path)))
        manager = MemoryManager(store)
        identity = RequestIdentity.for_user(user_id=user_id, session_id=session_id)
        saved = manager.save_explicit_for_identity(
            identity,
            text="记住我喜欢短回答。",
            content={
                "summary": "用户喜欢短回答。",
                "preference_key": "answer_style",
                "style": "concise",
            },
            source_intent="user_explicit",
        )
        result = manager.search_for_identity(
            identity,
            MemoryQuery(user_id=user_id, session_id=session_id, query="短回答", top_k=3),
        )
        passed = (
            type(store).__name__ == "SQLiteMemoryStore"
            and sqlite_path.exists()
            and getattr(saved, "memory_id", "")
            and any(item.memory_id == saved.memory_id for item in result.items)
        )
        return {
            "passed": bool(passed),
            "store": type(store).__name__,
            "file_exists": sqlite_path.exists(),
            "written_count": len(manager.list_for_identity(identity)),
            "search_result_count": len(result.items),
        }
    except Exception:
        return _failed_check("local_sqlite_core_failed")


def _check_dual_core_degradation(*, user_id: str, session_id: str) -> dict[str, Any]:
    try:
        identity = RequestIdentity.for_user(user_id=user_id, session_id=session_id)
        local_store = InMemoryStore()
        local_manager = MemoryManager(local_store)
        local_saved = local_manager.save_explicit_for_identity(
            identity,
            text="记住我喜欢短回答。",
            content={
                "summary": "用户喜欢短回答。",
                "preference_key": "answer_style",
                "style": "concise",
            },
            source_intent="user_explicit",
        )

        def failing_transport(request: MemoryServerRequest) -> Mapping[str, Any]:
            _ = request
            raise TimeoutError("memory server unavailable token=secret at http://memory.local")

        remote_client = _RemoteMemoryClient(base_url="http://memory.local", transport=failing_transport)
        manager = MemoryManager(HybridMemoryStore(local_store=local_store, remote_client=remote_client))
        context = manager.load_context_for_identity(identity, query_text="短回答", top_k=3)
        events = manager.list_audit_events_for_identity(
            identity,
            event_type="memory_remote_degraded",
            limit=1,
        )
        error_codes = list(context.recall_report.get("search_error_codes") or [])
        event_error_codes = list(events[0].metadata.get("error_codes") or []) if events else []
        passed = (
            getattr(local_saved, "memory_id", "")
            and any(item.memory_id == local_saved.memory_id for item in context.items)
            and "memory_server_query_failed" in error_codes
            and events
            and "memory_server_query_failed" in event_error_codes
        )
        return {
            "passed": bool(passed),
            "mode": "dual_core",
            "event_type": "memory_remote_degraded",
            "local_result_count": len(context.items),
            "error_codes": _stable_codes(error_codes),
        }
    except Exception:
        return _failed_check("dual_core_degradation_failed")


def _check_remote_service_lifecycle_failure(*, user_id: str, session_id: str) -> dict[str, Any]:
    try:
        identity = RequestIdentity.for_user(user_id=user_id, session_id=session_id)
        manager = MemoryManager(
            RemoteServiceMemoryStore(
                adapter=UnavailableRemoteMemoryServiceAdapter(base_url="http://memory.local"),
            )
        )
        operation_failed = False
        try:
            manager.save_explicit_for_identity(
                identity,
                text="记住我喜欢短回答。",
                content={
                    "summary": "用户喜欢短回答。",
                    "preference_key": "answer_style",
                    "style": "concise",
                },
                source_intent="user_explicit",
            )
        except MemoryServiceOperationError:
            operation_failed = True
        events = manager.list_audit_events_for_identity(
            identity,
            event_type="memory_remote_lifecycle_failed",
            limit=1,
        )
        error_code = str(events[0].metadata.get("error_code") or "") if events else ""
        passed = operation_failed and bool(events) and error_code == "memory_remote_service_save_explicit_failed"
        return {
            "passed": bool(passed),
            "mode": "remote_service",
            "event_type": "memory_remote_lifecycle_failed",
            "operation_failed": operation_failed,
            "error_codes": _stable_codes([error_code]),
        }
    except Exception:
        return _failed_check("remote_service_lifecycle_failure_failed")


def _check_memory_quality_eval() -> dict[str, Any]:
    try:
        cases = filter_cases_by_suite(load_cases(), "memory_quality")
        summary = run_evals(cases, router_mode="rule")
        quality = summary.get("memory_quality_eval") if isinstance(summary.get("memory_quality_eval"), dict) else {}
        passed = bool(cases) and summary.get("failed") == 0 and quality.get("action_accuracy") == 1.0
        return {
            "passed": passed,
            "suite": "memory_quality",
            "total": int(summary.get("total") or 0),
            "failed": int(summary.get("failed") or 0),
            "action_accuracy": float(quality.get("action_accuracy") or 0.0),
            "false_write_rate": float(quality.get("false_write_rate") or 0.0),
        }
    except Exception:
        return _failed_check("memory_quality_eval_failed")


def _check_external_memory_server(
    *,
    base_url: str,
    user_id: str,
    session_id: str,
    query_text: str,
    timeout_seconds: float,
    strategy: str,
) -> dict[str, Any]:
    try:
        client = RemoteMemoryClient(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            query_strategy=strategy,
            include_media_chunks=False,
            direct_answer=False,
        )
        health = client.health(user_id=user_id, session_id=session_id)
        result = client.query_memories(
            MemoryQuery(user_id=user_id, session_id=session_id, query=query_text, top_k=3),
        )
        error_codes = _stable_codes(error.get("code") for error in result.errors if isinstance(error, dict))
        health_status = str(health.get("status") or "")
        return {
            "passed": health_status.lower() == "ok" and not error_codes,
            "health_status": health_status,
            "health_version": str(health.get("version") or ""),
            "result_count": len(result.items),
            "error_codes": error_codes,
        }
    except Exception:
        return {
            "passed": False,
            "health_status": "failed",
            "health_version": "",
            "result_count": 0,
            "error_codes": ["memory_server_health_failed"],
        }


def _stable_codes(values: Any) -> list[str]:
    codes: list[str] = []
    if not values:
        return codes
    for value in values:
        code = str(value or "").strip()
        if code and code.startswith(("memory_server_", "memory_remote_service_")) and code not in codes:
            codes.append(code)
    return codes


def _failed_check(code: str) -> dict[str, Any]:
    return {"passed": False, "error_codes": [code]}


def _failed_check_codes(checks: Mapping[str, Mapping[str, Any]]) -> list[str]:
    codes: list[str] = []
    for name, check in checks.items():
        if check.get("passed") is True:
            continue
        check_codes = _stable_codes(check.get("error_codes") or [])
        codes.extend(check_codes or [f"{name}_failed"])
    return codes


if __name__ == "__main__":
    raise SystemExit(main())
