from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from langgraph.checkpoint.memory import MemorySaver
import pytest

from assistant_agent.runtime.assistant_graph_app import GraphExecutionIdentity
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import (
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
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


def test_runtime_reuses_public_graph_without_writing_m1_turn_checkpoints() -> None:
    """M1 runs reuse the graph but do not claim checkpointer-backed turn isolation."""

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
        assert saver.storage == {}
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
        assert saver.storage == {}
    finally:
        runtime.close()


def test_concurrent_runs_share_compiled_graph_without_checkpoint_cross_talk() -> None:
    """Concurrent consumers complete independently without creating M1 checkpoints."""

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
        assert saver.storage == {}
    finally:
        runtime.close()
