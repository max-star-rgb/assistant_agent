"""Prompt-safe observability wrapper for fixed graph memory nodes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from time import perf_counter
from typing import Any, Literal

from assistant_agent.observability.trace_store import append_observability_event


MemoryNodePhase = Literal["recall", "commit"]


def observe_memory_node(
    node: Callable[[Any, Any], Any],
    *,
    backend_id: str,
    phase: MemoryNodePhase,
) -> Callable[[Any, Any], Any]:
    """Record structural node outcome facts without memory or conversation text."""

    def wrapped(state: Any, runtime: Any) -> Any:
        started_at = perf_counter()
        try:
            result = node(state, runtime)
        except BaseException:
            _append_fail_open(
                state,
                runtime,
                backend_id=backend_id,
                phase=phase,
                status="failed",
                latency_ms=_latency_ms(started_at),
                attributes={"backend_id": backend_id, "issue_code": "node_error"},
            )
            raise
        status, attributes = _outcome(result, backend_id=backend_id, phase=phase)
        _append_fail_open(
            result,
            runtime,
            backend_id=backend_id,
            phase=phase,
            status=status,
            latency_ms=_latency_ms(started_at),
            attributes=attributes,
        )
        return result

    return wrapped


def _outcome(
    state: Mapping[str, Any], *, backend_id: str, phase: MemoryNodePhase
) -> tuple[str, dict[str, Any]]:
    if phase == "recall":
        context = state.get("memory_context") or {}
        items = context.get("items") or ()
        issue_codes = list(context.get("issue_codes") or ())
        return str(context.get("status") or "failed"), {
            "backend_id": backend_id,
            "item_count": len(items),
            "char_count": sum(len(str(item.get("text") or "")) for item in items),
            "issue_codes": issue_codes,
        }
    commit = state.get("memory_commit") or {}
    return str(commit.get("status") or "failed"), {
        "backend_id": backend_id,
        "memory_event_id": commit.get("memory_event_id"),
        "issue_code": commit.get("issue_code"),
    }


def _append_fail_open(
    state: Mapping[str, Any],
    runtime: Any,
    *,
    backend_id: str,
    phase: MemoryNodePhase,
    status: str,
    latency_ms: int,
    attributes: dict[str, Any],
) -> None:
    context = getattr(runtime, "context", None)
    trace_store = getattr(context, "trace_store", None)
    if trace_store is None:
        return
    run = state.get("run") or {}
    request = state.get("request") or {}
    try:
        append_observability_event(
            trace_store,
            trace_id=str(run["trace_id"]),
            run_id=str(run["run_id"]),
            user_id=str(request["user_id"]),
            session_id=str(request["session_id"]),
            canonical_event=f"memory.{phase}.finished",
            observation_type="span",
            observation_name=f"memory.{phase}",
            node_name=f"memory_{phase}",
            status=status,
            latency_ms=latency_ms,
            attributes=attributes,
        )
    except Exception:
        return


def _latency_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


__all__ = ["observe_memory_node"]
