from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
import pytest

from assistant_agent.runtime.assistant_graph_app import (
    AssistantTurnGraphApp,
    GraphExecutionError,
    GraphExecutionIdentity,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.assistant_graph_state import ASSISTANT_GRAPH_NAME
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.tool_executor import ToolExecutor
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


class _AstreamProbe:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.input_state: dict[str, Any] | None = None
        self.kwargs: dict[str, Any] = {}

    async def astream(
        self,
        input_state: dict[str, Any],
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.input_state = input_state
        self.kwargs = kwargs
        for event in self.events:
            yield event


def _identity(run_id: str = "run-sentinel") -> GraphExecutionIdentity:
    return GraphExecutionIdentity.for_assistant_turn(
        agent_id="agent-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        run_id=run_id,
    )


def _context() -> GraphRuntimeContext:
    return GraphRuntimeContext(
        tool_executor=ToolExecutor(registry=sealed_registry()),
        chat_adapter=ScriptedChatAdapter([]),
    )


def _request(text: str) -> UserRequest:
    return UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text=text,
    )


def _runtime(*, checkpointer: MemorySaver | None = None) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="first-sentinel",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="second-sentinel",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=checkpointer,
    )


def test_identity_has_stable_thread_without_virtual_checkpoint_namespace() -> None:
    """Changing a turn must preserve its conversation thread without inventing a saver key."""

    one = GraphExecutionIdentity.for_assistant_turn(
        agent_id="a", user_id="u", session_id="s", run_id="r1"
    )
    two = GraphExecutionIdentity.for_assistant_turn(
        agent_id="a", user_id="u", session_id="s", run_id="r2"
    )

    assert one.thread_id == two.thread_id
    assert one.run_id == "r1"
    assert two.run_id == "r2"
    assert one.runnable_config() == {
        "configurable": {"thread_id": one.thread_id, "run_id": "r1"}
    }


def test_runtime_exposes_compiled_graph_as_read_only() -> None:
    """Consumers can use the app graph but cannot replace the shared compilation."""

    runtime = _runtime()
    try:
        with pytest.raises(AttributeError):
            runtime.assistant_graph_app.graph = object()
    finally:
        runtime.close()


def test_runtime_reuses_public_graph_and_checkpoints_latest_turn() -> None:
    """M2 keeps one compiled graph and checkpoints the latest conversation turn."""

    saver = MemorySaver()
    runtime = _runtime(checkpointer=saver)
    try:
        graph = runtime.assistant_graph_app.graph

        first = runtime.run_state(_request("one"), run_id="run-one")
        second = runtime.run_state(_request("two"), run_id="run-two")

        assert first.response is not None
        assert first.response.message == "first-sentinel"
        assert second.response is not None
        assert second.response.message == "second-sentinel"
        assert runtime.assistant_graph_app.graph is graph
        identity = GraphExecutionIdentity.for_assistant_turn(
            agent_id=second.agent_id,
            user_id=second.user_id,
            session_id=second.session_id,
            run_id=second.run_id,
        )
        snapshot = graph.get_state(identity.runnable_config()).values
        assert snapshot["graph_name"] == ASSISTANT_GRAPH_NAME
        assert snapshot["run"]["run_id"] == "run-two"
        assert snapshot["final_response"]["message"] == "second-sentinel"
    finally:
        runtime.close()


def test_runtime_does_not_reuse_prior_turn_tool_observation_in_stable_thread() -> None:
    """A new run must not inherit tool observations from an earlier turn."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="call-one",
                        name=ProbeTool.name,
                        arguments={"value": "first-value"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="first-sentinel",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="second-sentinel",
            ),
        ]
    )
    saver = MemorySaver()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    try:
        runtime.run_state(_request("first"), run_id="run-one")
        runtime.run_state(_request("second"), run_id="run-two")

        assert [message["role"] for message in adapter.requests[2].messages] == [
            "system",
            "user",
        ]
        identity = GraphExecutionIdentity.for_assistant_turn(
            agent_id=runtime.agent_id,
            user_id="user-sentinel",
            session_id="session-sentinel",
            run_id="run-two",
        )
        snapshot = runtime.assistant_graph_app.graph.get_state(
            identity.runnable_config()
        ).values
        assert snapshot["run"]["run_id"] == "run-two"
        assert snapshot["tool_observations"] == []
        assert snapshot["run"]["tool_results"] == []
    finally:
        runtime.close()


def test_concurrent_callers_receive_independent_results_on_shared_graph() -> None:
    """Gateway serialization owns same-thread ordering; callers still get their own result."""

    saver = MemorySaver()
    runtime = _runtime(checkpointer=saver)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(runtime.run_state, _request(text), run_id=run_id)
                for text, run_id in (("one", "run-one"), ("two", "run-two"))
            ]
        states = [future.result() for future in futures]

        assert {state.run_id for state in states} == {"run-one", "run-two"}
        assert {state.status for state in states} == {"completed"}
        identity = GraphExecutionIdentity.for_assistant_turn(
            agent_id=runtime.agent_id,
            user_id="user-sentinel",
            session_id="session-sentinel",
            run_id="run-two",
        )
        snapshot = runtime.assistant_graph_app.graph.get_state(
            identity.runnable_config()
        ).values
        assert snapshot["graph_name"] == ASSISTANT_GRAPH_NAME
        assert snapshot["run"]["run_id"] in {"run-one", "run-two"}
    finally:
        runtime.close()


def test_astream_normalizes_v2_events_and_preserves_subgraph_namespace() -> None:
    """Changing the consumer to v1 or dropping ``ns`` must break the public stream."""

    probe = _AstreamProbe(
        [
            {"type": "updates", "ns": ("worker:1",), "data": {"tool": {}}},
            {"type": "values", "ns": (), "data": {"state": "final"}},
        ]
    )
    app = AssistantTurnGraphApp.from_compiled_graph(probe)
    context = _context()

    async def collect_parts() -> list[Any]:
        return [
            part
            async for part in app.astream(
                {"state": "initial"},
                identity=_identity(),
                context=context,
            )
        ]

    parts = asyncio.run(collect_parts())

    assert [(part.type, part.namespace, part.data) for part in parts] == [
        ("updates", ("worker:1",), {"tool": {}}),
        ("values", (), {"state": "final"}),
    ]
    assert probe.input_state == {"state": "initial"}
    assert probe.kwargs == {
        "config": {
            **_identity().runnable_config(),
            "metadata": {
                "run_id": "run-sentinel",
                "thread_id": _identity().thread_id,
                "agent_id": "agent-sentinel",
                "execution_engine": "assistant_turn_graph",
            },
            "tags": ["assistant_turn_graph"],
            "callbacks": [],
        },
        "context": context,
        "stream_mode": [
            "values",
            "updates",
            "messages",
            "custom",
            "tasks",
            "checkpoints",
        ],
        "subgraphs": True,
        "version": "v2",
    }


def test_arun_returns_only_the_last_root_values_as_final_state() -> None:
    """A later subgraph value must not replace the root graph's final state."""

    probe = _AstreamProbe(
        [
            {"type": "values", "ns": ("worker:1",), "data": {"state": "worker"}},
            {"type": "values", "ns": (), "data": {"state": "root-initial"}},
            {"type": "updates", "ns": (), "data": {"node": {}}},
            {"type": "values", "ns": (), "data": {"state": "root-final"}},
            {"type": "values", "ns": ("worker:2",), "data": {"state": "later-worker"}},
        ]
    )
    app = AssistantTurnGraphApp.from_compiled_graph(probe)

    result = asyncio.run(
        app.arun(
            {"state": "initial"},
            identity=_identity(),
            context=_context(),
        )
    )

    assert result.final_state == {"state": "root-final"}
    assert [(part.type, part.namespace) for part in result.parts] == [
        ("values", ("worker:1",)),
        ("values", ()),
        ("updates", ()),
        ("values", ()),
        ("values", ("worker:2",)),
    ]


def test_arun_fails_closed_when_root_final_values_are_missing() -> None:
    """A subgraph result alone must never be returned as a completed root run."""

    probe = _AstreamProbe(
        [
            {"type": "updates", "ns": (), "data": {"node": {}}},
            {"type": "values", "ns": ("worker:1",), "data": {"state": "worker"}},
        ]
    )
    app = AssistantTurnGraphApp.from_compiled_graph(probe)

    with pytest.raises(GraphExecutionError) as captured:
        asyncio.run(
            app.arun(
                {"state": "initial"},
                identity=_identity(),
                context=_context(),
            )
        )

    assert captured.value.code == "graph_final_state_missing"
