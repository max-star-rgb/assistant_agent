"""Trace helpers for prompt-safe response lifecycle events."""

from __future__ import annotations

from assistant_agent.runtime.state import AgentState
from assistant_agent.observability.trace_store import TraceStore, append_observability_event


def append_response_final_event(
    *,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
    source: str,
    latency_ms: int | None = None,
) -> None:
    """Append a prompt-safe final response trace event when a response exists."""

    response = state.response
    if response is None:
        return
    message = response.message or ""
    data = response.data if isinstance(response.data, dict) else {}
    output_refs = response.output_refs or []
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="response.final",
        observation_type="span",
        node_name=node_name,
        status=_response_status(state.status),
        latency_ms=latency_ms,
        attributes={
            "source": source,
            "message_present": bool(message),
            "message_chars": len(message),
            "followup_present": response.followup_question is not None,
            "output_ref_count": len(output_refs),
            "response_data_keys": sorted(str(key) for key in data.keys()),
            "error_count": len(state.errors),
        },
        output_summary={
            "response": {
                "message_present": bool(message),
                "message_chars": len(message),
                "followup_present": response.followup_question is not None,
                "output_ref_count": len(output_refs),
                "output_refs": output_refs[:8],
                "data_keys": sorted(str(key) for key in data.keys()),
            }
        },
    )


def _response_status(state_status: str) -> str:
    if state_status in {"failed", "cancelled"}:
        return state_status
    return "succeeded"
