"""Graph execution trace storage."""

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field


TraceEventType = Literal["node_started", "node_finished", "tool_failed"]
GraphStateT = TypeVar("GraphStateT", bound=dict[str, Any])


class TraceEvent(BaseModel):
    """Serializable trace event for one graph execution point."""

    trace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    node_name: str = Field(min_length=1)
    event_type: TraceEventType
    before_state_summary: dict[str, Any] = Field(default_factory=dict)
    after_state_summary: dict[str, Any] = Field(default_factory=dict)
    tool_name: str | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TraceStore(Protocol):
    """Destination for graph trace events."""

    def append(self, event: TraceEvent) -> None:
        """Persist one trace event."""

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        """Return trace events for a run in insertion order."""

    def node_path(self, run_id: str) -> list[str]:
        """Return finished graph node names for a run."""


class InMemoryTraceStore:
    """In-memory trace store for tests and local debugging."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def append(self, event: TraceEvent) -> None:
        self.events.append(event)

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        return [event for event in self.events if event.run_id == run_id]

    def node_path(self, run_id: str) -> list[str]:
        return [event.node_name for event in self.list_by_run(run_id) if event.event_type == "node_finished"]


class JsonlTraceStore:
    """JSONL-backed trace store."""

    def __init__(self, path: Path | str = ".data/graph_trace.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: TraceEvent) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        if not self.path.exists():
            return []
        events: list[TraceEvent] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    event = TraceEvent.model_validate_json(line)
                    if event.run_id == run_id:
                        events.append(event)
        return events

    def node_path(self, run_id: str) -> list[str]:
        return [event.node_name for event in self.list_by_run(run_id) if event.event_type == "node_finished"]


def new_trace_id() -> str:
    """Create a new graph trace identifier."""

    return f"trace_{uuid4().hex}"


def trace_graph_node(node_name: str, node_func: Callable[[GraphStateT], GraphStateT]) -> Callable[[GraphStateT], GraphStateT]:
    """Wrap a LangGraph node and record started/finished trace events."""

    def wrapped(graph_state: GraphStateT) -> GraphStateT:
        trace_store = graph_state.get("trace_store")
        trace_id = graph_state.get("trace_id")
        state = graph_state.get("state")
        run_id = getattr(state, "run_id", None)
        before = summarize_graph_state(graph_state)
        if trace_store is not None and trace_id is not None and run_id is not None:
            trace_store.append(
                TraceEvent(
                    trace_id=trace_id,
                    run_id=run_id,
                    node_name=node_name,
                    event_type="node_started",
                    before_state_summary=before,
                )
            )

        previous_node = graph_state.get("current_node_name")
        graph_state["current_node_name"] = node_name
        error: dict[str, Any] | None = None
        try:
            result = node_func(graph_state)
        except Exception as exc:
            error = {"code": "unknown_error", "message": sanitize_trace_value(str(exc))}
            raise
        finally:
            if previous_node is None:
                graph_state.pop("current_node_name", None)
            else:
                graph_state["current_node_name"] = previous_node
            after = summarize_graph_state(graph_state)
            if trace_store is not None and trace_id is not None and run_id is not None:
                trace_store.append(
                    TraceEvent(
                        trace_id=trace_id,
                        run_id=run_id,
                        node_name=node_name,
                        event_type="node_finished",
                        before_state_summary=before,
                        after_state_summary=after,
                        error=error or latest_error_summary(graph_state),
                    )
                )
        return result

    return wrapped


def summarize_graph_state(graph_state: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, non-sensitive graph state summary."""

    state = graph_state.get("state")
    if state is None:
        return {}
    return {
        "status": getattr(state, "status", None),
        "intent": state.intent.intent if getattr(state, "intent", None) is not None else None,
        "plan_step_count": len(state.plan.steps) if getattr(state, "plan", None) is not None else 0,
        "selected_tool_count": len(getattr(state, "selected_tools", [])),
        "tool_call_count": len(getattr(state, "tool_calls", [])),
        "tool_result_count": len(getattr(state, "tool_results", [])),
        "error_count": len(getattr(state, "errors", [])),
        "current_step_index": graph_state.get("current_step_index", 0),
    }


def latest_error_summary(graph_state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest structured error summary, if present."""

    state = graph_state.get("state")
    errors = getattr(state, "errors", [])
    if not errors:
        return None
    error = errors[-1]
    return {
        "source": error.source,
        "code": error.details.get("code", "unknown_error"),
        "message": sanitize_trace_value(error.message),
        "recovery_action": error.details.get("recovery_action"),
    }


def sanitize_trace_value(value: str) -> str:
    """Remove obvious secret material and cap trace text size."""

    secret_markers = ("api_key", "apikey", "authorization", "bearer", "secret", "token", "password")
    compact = " ".join(value.strip().split())
    words = []
    redact_next = False
    for word in compact.split(" "):
        lowered = word.lower()
        if redact_next or lowered.startswith(("sk-", "pk-")) or any(marker in lowered for marker in secret_markers):
            words.append("[redacted]")
            redact_next = lowered in {"bearer", "authorization", "token", "password"}
        else:
            words.append(word)
            redact_next = False
    sanitized = " ".join(words)
    if len(sanitized) > 300:
        return f"{sanitized[:297]}..."
    return sanitized
