from __future__ import annotations

import inspect
import json

from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.plugins.contracts import MemoryContextItem
from assistant_agent.runtime.assistant_graph_state import (
    assistant_turn_state_from_agent_state,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.workflows.durable_graph import build_durable_workflow_graph


def test_workflow_graph_does_not_compile_an_unused_store() -> None:
    """A future compile(store=...) call must first have a governed consumer."""

    assert "store" not in inspect.signature(build_durable_workflow_graph).parameters


def test_long_term_memory_content_is_not_checkpointed() -> None:
    """Persisting memory text would bypass the MemoryPluginHost lifecycle."""

    state = AgentState.from_request(
        UserRequest(
            user_id="user-memory-boundary",
            session_id="session-memory-boundary",
            text="current request",
        ),
        run_id="run-memory-boundary",
        trace_id="trace-memory-boundary",
        agent_id="agent-memory-boundary",
    )
    state.session_memory_snapshot = SessionMemorySnapshot(
        memories=[
            MemoryContextItem(
                memory_id="memory-ref-1",
                text="long-term-content-must-remain-host-owned",
                source="long_term",
            )
        ],
        plugin_id="memory-plugin-probe",
    )

    checkpoint = assistant_turn_state_from_agent_state(state)
    encoded = json.dumps(checkpoint, sort_keys=True)

    assert checkpoint["context_refs"] == [
        {
            "kind": "memory",
            "ref": "memory-ref-1",
            "source": "long_term",
            "version": None,
            "status_code": None,
        }
    ]
    assert "long-term-content-must-remain-host-owned" not in encoded
    assert "memory-plugin-probe" not in encoded
