from __future__ import annotations

from dataclasses import fields
import pytest

from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.runtime.assistant_graph_state import (
    MemoryCommitState,
    MemoryContext,
    MemoryContextItem,
    assistant_turn_state_from_request,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import ScriptedChatAdapter, offline_config, sealed_registry


class _MemoryProbe:
    def __init__(self) -> None:
        self.recall_calls = 0
        self.commit_calls = 0
        self.memory_text = "frozen-memory"

    def recall_node(self, state, runtime):
        del runtime
        validated = validate_assistant_turn_state(state)
        if validated.get("memory_context") is not None:
            return validated
        self.recall_calls += 1
        updated = dict(validated)
        updated["memory_context"] = MemoryContext(
            backend_id="probe",
            status="ready",
            snapshot_id=f"snapshot-{self.recall_calls}",
            items=(
                MemoryContextItem(
                    memory_id="memory-sentinel",
                    text=self.memory_text,
                    source="probe",
                    relevance=1.0,
                ),
            ),
        ).model_dump(mode="json")
        return validate_assistant_turn_state(updated)

    def commit_node(self, state, runtime):
        del runtime
        validated = validate_assistant_turn_state(state)
        updated = dict(validated)
        if validated["turn_provenance"] == "time_travel":
            updated["memory_commit"] = MemoryCommitState(
                status="skipped",
                issue_code="time_travel_commit_disabled",
            ).model_dump(mode="json")
            return validate_assistant_turn_state(updated)
        self.commit_calls += 1
        updated["memory_commit"] = MemoryCommitState(
            status="succeeded",
            memory_event_id=f"event-{validated['turn_origin_id']}",
        ).model_dump(mode="json")
        return validate_assistant_turn_state(updated)


def _bundle(client: _MemoryProbe) -> MemoryNodeBundle:
    return MemoryNodeBundle(
        backend_id="probe",
        recall_node=client.recall_node,
        commit_node=client.commit_node,
    )


def _completed_state():
    state = assistant_turn_state_from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="request"),
        run_id="origin-run",
        trace_id="trace-1",
        agent_id="agent-1",
    )
    state["run"]["status"] = "completed"
    state["final_response"] = {
        "message": "response",
        "followup_question": None,
        "output_refs": [],
        "citations": [],
    }
    state["response_publish"] = {
        "status": "published",
        "final_fact_id": "fact-1",
        "issue_code": None,
    }
    return state


@pytest.mark.core_invariant("MEMORY-001")
def test_memory_bundle_is_thin_and_runtime_graph_owns_recall_commit_order(
    tmp_path,
) -> None:
    client = _MemoryProbe()
    bundle = _bundle(client)
    sink = ListEventSink()
    original_commit = bundle.commit_node

    def commit_after_publish(state, runtime):
        assert any(event.type == "final_response" for event in sink.events)
        return original_commit(state, runtime)

    guarded = MemoryNodeBundle(
        backend_id=bundle.backend_id,
        recall_node=bundle.recall_node,
        commit_node=commit_after_publish,
        store=bundle.store,
        aclose=bundle.aclose,
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="response",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        memory_bundle=guarded,
    )
    try:
        state = runtime.run_state(
            UserRequest(user_id="user-1", session_id="session-1", text="request"),
            event_sink=sink,
            run_id="run-1",
        )
        graph = runtime.assistant_graph_app.graph.get_graph()
    finally:
        runtime.close()

    assert tuple(field.name for field in fields(MemoryNodeBundle)) == (
        "backend_id",
        "recall_node",
        "commit_node",
        "store",
        "aclose",
    )
    assert state.status == "completed"
    assert client.recall_calls == 1
    assert client.commit_calls == 1
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert {"memory_recall", "publish_response", "memory_commit"}.issubset(
        graph.nodes
    )
    assert "time_travel_anchor" not in graph.nodes
    assert ("memory_recall", "assistant") in edges
    assert ("compose_response", "publish_response") in edges
    assert ("publish_response", "memory_commit") in edges
    assert ("memory_commit", "__end__") in edges


@pytest.mark.core_invariant("MEMORY-001")
def test_snapshot_is_frozen_and_derived_history_never_writes(tmp_path) -> None:
    client = _MemoryProbe()
    bundle = _bundle(client)
    original = bundle.recall_node(_completed_state(), None)
    original_snapshot = original["memory_context"]
    client.memory_text = "new-backend-value"

    for kind in ("replay", "fork"):
        derived = dict(original)
        derived["turn_provenance"] = "time_travel"
        recalled = bundle.recall_node(derived, None)
        committed = bundle.commit_node(recalled, None)
        assert recalled["memory_context"] == original_snapshot
        assert committed["memory_commit"]["issue_code"] == (
            "time_travel_commit_disabled"
        )

    assert client.recall_calls == 1
    assert client.commit_calls == 0
