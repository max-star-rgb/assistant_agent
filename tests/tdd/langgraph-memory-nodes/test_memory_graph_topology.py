from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace

import pytest
from langgraph.store.memory import InMemoryStore

from assistant_agent.memory.backends.disabled import build_disabled_memory_bundle
from assistant_agent.memory.node_bundle import MemoryNodeBundle
from assistant_agent.runtime.assistant_graph_state import (
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph
from assistant_agent.runtime.requests import UserRequest


def _new_state():
    return assistant_turn_state_from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="hello"),
        run_id="run-1",
        trace_id="trace-1",
    )


def test_memory_node_bundle_is_a_closed_frozen_composition_value() -> None:
    bundle = build_disabled_memory_bundle()

    assert isinstance(bundle, MemoryNodeBundle)
    assert tuple(field.name for field in fields(bundle)) == (
        "backend_id",
        "recall_node",
        "commit_node",
        "store",
        "aclose",
    )
    with pytest.raises(FrozenInstanceError):
        bundle.backend_id = "mutated"  # type: ignore[misc]


def test_disabled_nodes_return_explicit_empty_and_skipped_state() -> None:
    state = _new_state()
    bundle = build_disabled_memory_bundle()

    recalled = bundle.recall_node(state, None)
    committed = bundle.commit_node(recalled, None)

    assert recalled["memory_context"] == {
        "schema_version": 1,
        "backend_id": "disabled",
        "status": "empty",
        "snapshot_id": (
            "disabled:f17a6a0bb899d928ce485832f9cb59cb"
            "faf71097d7f363e13a2f246f2154aa99"
        ),
        "items": [],
        "issue_codes": [],
    }
    assert committed["memory_commit"] == {
        "status": "skipped",
        "memory_event_id": None,
        "issue_code": "memory_disabled",
    }


def test_compiled_graph_has_fixed_memory_nodes_behind_reentry_gate() -> None:
    store = InMemoryStore()
    bundle = replace(build_disabled_memory_bundle(), store=store)

    compiled = build_assistant_loop_graph(memory_bundle=bundle)
    drawable = compiled.get_graph()
    edges = {(edge.source, edge.target) for edge in drawable.edges}

    assert compiled.store is store
    assert {
        "memory_recall",
        "publish_response",
        "memory_commit",
    }.issubset(drawable.nodes)
    assert ("memory_recall", "time_travel_anchor") in edges
    assert ("publish_response", "time_travel_anchor") in edges
    assert ("memory_commit", "time_travel_anchor") in edges
    assert ("time_travel_anchor", "prepare_invocation") in edges
    assert ("prepare_invocation", "memory_recall") in edges
    assert ("prepare_invocation", "publish_response") in edges
    assert ("prepare_invocation", "memory_commit") in edges


def test_new_turn_starts_at_memory_recall() -> None:
    assert _new_state()["continuation"] == "memory_recall"
