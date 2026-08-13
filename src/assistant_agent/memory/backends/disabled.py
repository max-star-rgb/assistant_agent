"""Explicit no-op memory backend used when long-term memory is disabled."""

from __future__ import annotations

import hashlib
from typing import Any

from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.runtime.assistant_graph_state import (
    MemoryCommitState,
    MemoryContext,
    validate_assistant_turn_state,
)


def disabled_memory_recall_node(state: Any, runtime: Any) -> Any:
    """Freeze an explicit empty snapshot without touching external resources."""

    validated = validate_assistant_turn_state(state)
    context = getattr(runtime, "context", None)
    invocation_kind = str(
        getattr(context, "invocation_kind", validated["invocation_kind"])
    )
    refresh_memory = invocation_kind == "fork" and bool(
        getattr(context, "refresh_memory", False)
    )
    if validated.get("memory_context") is not None and not refresh_memory:
        return validated
    if invocation_kind in {"resume", "replay", "fork"} and not refresh_memory:
        from assistant_agent.runtime.assistant_graph_state import (
            AssistantStateCompatibilityError,
        )

        raise AssistantStateCompatibilityError(
            "Continuation checkpoint has no frozen memory_context."
        )
    origin = validated["turn_origin_id"]
    digest = hashlib.sha256(f"disabled\0{origin}".encode("utf-8")).hexdigest()
    updated = dict(validated)
    updated["memory_context"] = MemoryContext(
        backend_id="disabled",
        status="empty",
        snapshot_id=f"disabled:{digest}",
    ).model_dump(mode="json")
    return validate_assistant_turn_state(updated)


def disabled_memory_commit_node(state: Any, runtime: Any) -> Any:
    """Record that the configured backend intentionally performs no write."""

    validated = validate_assistant_turn_state(state)
    context = getattr(runtime, "context", None)
    invocation_kind = str(
        getattr(context, "invocation_kind", validated["invocation_kind"])
    )
    updated = dict(validated)
    updated["memory_commit"] = MemoryCommitState(
        status="skipped",
        issue_code=(
            "time_travel_commit_disabled"
            if invocation_kind in {"replay", "fork"}
            or validated["turn_provenance"] == "time_travel"
            else "memory_disabled"
        ),
    ).model_dump(mode="json")
    return validate_assistant_turn_state(updated)


def build_disabled_memory_bundle() -> MemoryNodeBundle:
    return MemoryNodeBundle(
        backend_id="disabled",
        recall_node=disabled_memory_recall_node,
        commit_node=disabled_memory_commit_node,
    )


__all__ = [
    "build_disabled_memory_bundle",
    "disabled_memory_commit_node",
    "disabled_memory_recall_node",
]
