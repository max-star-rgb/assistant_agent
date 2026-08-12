from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.runtime.assistant_graph_state import (
    AssistantStateCompatibilityError,
    assistant_turn_state_from_agent_state,
    reenter_assistant_invocation,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_invocation_claims import (
    GraphInvocationClaimConflict,
    InMemoryGraphInvocationClaimStore,
)
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


CLAIM = {
    "owner_digest": "owner-digest",
    "thread_id": "thread-claim",
    "run_id": "run-claim",
    "invocation_kind": "replay",
}


def _request(*, text: str = "run the probe") -> UserRequest:
    return UserRequest(
        user_id="user-reentry",
        session_id="session-reentry",
        text=text,
    )


def _runtime_state(*, run_id: str, trace_id: str = "trace-origin") -> AgentState:
    request = _request()
    request.metadata["_trusted_graph_profile"] = "standard"
    return AgentState.from_request(
        request,
        run_id=run_id,
        trace_id=trace_id,
    )


def test_claim_store_distinguishes_same_invocation_from_competing_branch() -> None:
    """Replacing the token value with a run-id set would admit competing branches."""

    store = InMemoryGraphInvocationClaimStore()

    assert store.claim(**CLAIM, invocation_token="token-a") == "claimed"
    assert store.claim(**CLAIM, invocation_token="token-a") == "same_invocation"
    with pytest.raises(GraphInvocationClaimConflict) as captured:
        store.claim(**CLAIM, invocation_token="token-b")

    assert captured.value.code == "graph_invocation_run_id_reused"


def test_prepare_invocation_reenters_same_turn_with_new_run_id() -> None:
    """Keeping the checkpoint run id would bind Replay/Resume to an old invocation."""

    prepared_state = assistant_turn_state_from_agent_state(
        _runtime_state(run_id="run-original")
    )
    runtime_state = _runtime_state(run_id="run-replay-new")

    updated = reenter_assistant_invocation(
        prepared_state,
        runtime_state=runtime_state,
        invocation_kind="replay",
    )

    assert updated["turn_origin_id"] == prepared_state["turn_origin_id"]
    assert updated["invocation_run_id"] == "run-replay-new"
    assert updated["invocation_run_ids"] == ["run-original", "run-replay-new"]
    assert updated["invocation_kind"] == "replay"
    assert updated["run"]["run_id"] == "run-replay-new"
    assert updated["run"]["trace_id"] == prepared_state["run"]["trace_id"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state["request"].__setitem__("user_id", "other-owner"),
        lambda state: state["request"].__setitem__("text", "other-request"),
        lambda state: state.__setitem__("profile", "worker"),
        lambda state: state.__setitem__("state_schema_version", 999),
        lambda state: state["run"].__setitem__("trace_id", "other-trace"),
    ],
)
def test_reentry_fails_closed_on_checkpoint_runtime_mismatch(mutation) -> None:
    """Weak re-entry validation could run historical work for the wrong invocation owner."""

    persisted = assistant_turn_state_from_agent_state(
        _runtime_state(run_id="run-original")
    )
    mutated = deepcopy(persisted)
    mutation(mutated)

    with pytest.raises(AssistantStateCompatibilityError):
        reenter_assistant_invocation(
            mutated,
            runtime_state=_runtime_state(run_id="run-new"),
            invocation_kind="replay",
        )


def test_diagnostic_run_ids_do_not_replace_atomic_claim_store() -> None:
    """Historical diagnostics must not reject a same-token graph-node re-entry."""

    state = assistant_turn_state_from_agent_state(_runtime_state(run_id="run-loop"))
    first = reenter_assistant_invocation(
        state,
        runtime_state=_runtime_state(run_id="run-loop"),
        invocation_kind="invoke",
    )
    second = reenter_assistant_invocation(
        first,
        runtime_state=_runtime_state(run_id="run-loop"),
        invocation_kind="invoke",
    )

    assert second["invocation_run_ids"] == ["run-loop"]


def test_real_tool_stream_crosses_gate_before_every_semantic_node() -> None:
    """Any semantic edge bypassing the anchor/gate must change this real update order."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="provider-reentry",
                            name=ProbeTool.name,
                            arguments={"value": "gate-sentinel"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="done-reentry",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    prepared = runtime._prepare_graph_run(  # noqa: SLF001 - native stream TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        trace_context=None,
        export_trace_context=None,
        pre_terminal_state_hook=None,
        run_id="run-loop",
    )

    async def exercise() -> tuple[list[str], dict[str, Any]]:
        result = await runtime.assistant_graph_app.arun(
            prepared.initial_state,
            identity=prepared.identity,
            context=prepared.runtime_context,
        )
        order: list[str] = []
        for part in result.parts:
            if part.type != "updates" or part.namespace or not isinstance(part.data, dict):
                continue
            order.extend(str(node) for node in part.data)
        return order, result.final_state

    try:
        order, final_state = asyncio.run(exercise())
    finally:
        runtime.close()

    assert order == [
        "prepare_invocation",
        "assistant",
        "time_travel_anchor",
        "prepare_invocation",
        "execute_tool",
        "time_travel_anchor",
        "prepare_invocation",
        "assistant",
        "time_travel_anchor",
        "prepare_invocation",
        "compose_response",
        "time_travel_anchor",
        "prepare_invocation",
    ]
    assert final_state["continuation"] == "end"
    assert final_state["invocation_run_ids"].count("run-loop") == 1
    assert final_state["run"]["status"] == "completed"


def test_graph_topology_routes_semantic_nodes_only_through_anchor_and_gate() -> None:
    """A direct semantic edge could execute Provider/Tool/compose before re-entry."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
    )
    try:
        drawable = runtime.assistant_graph_app.graph.get_graph()
    finally:
        runtime.close()

    edges = {(edge.source, edge.target) for edge in drawable.edges}
    semantic = {"assistant", "await_input", "execute_tool", "compose_response"}
    assert ("__start__", "prepare_invocation") in edges
    assert ("time_travel_anchor", "prepare_invocation") in edges
    assert all((node, "time_travel_anchor") in edges for node in semantic)
    assert not any(source in semantic and target in semantic for source, target in edges)
    assert not any(source in semantic and target == "__end__" for source, target in edges)
