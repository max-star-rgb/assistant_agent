from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
import pytest

from assistant_agent.runtime.assistant_graph_app import GraphExecutionIdentity
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_runtime import RUNTIME_STATE_KEYS
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


def _runtime() -> AgentGraphRuntime:
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
        checkpointer=MemorySaver(),
    )


def test_identity_has_stable_thread_and_run_namespace() -> None:
    """Changing a turn must not change the conversation checkpoint thread."""

    one = GraphExecutionIdentity.for_assistant_turn(
        agent_id="a", user_id="u", session_id="s", run_id="r1"
    )
    two = GraphExecutionIdentity.for_assistant_turn(
        agent_id="a", user_id="u", session_id="s", run_id="r2"
    )

    assert one.thread_id == two.thread_id
    assert one.checkpoint_ns == "turn:r1"
    assert two.checkpoint_ns == "turn:r2"
    assert one.runnable_config()["configurable"]["run_id"] == "r1"


def test_runtime_exposes_compiled_graph_as_read_only() -> None:
    """Consumers can use the app graph but cannot replace the shared compilation."""

    runtime = _runtime()
    try:
        with pytest.raises(AttributeError):
            runtime.assistant_graph_app.graph = object()
    finally:
        runtime.close()


def test_runtime_reuses_public_graph_and_keeps_turn_context_out_of_checkpoints() -> None:
    """A shared Runtime preserves its graph while each turn has clean saved state."""

    runtime = _runtime()
    try:
        graph = runtime.assistant_graph_app.graph

        first = runtime.run_state(_request("one"), run_id="run-one")
        second = runtime.run_state(_request("two"), run_id="run-two")

        assert first.response is not None
        assert first.response.message == "first-sentinel"
        assert second.response is not None
        assert second.response.message == "second-sentinel"
        assert runtime.assistant_graph_app.graph is graph

        for state in (first, second):
            identity = GraphExecutionIdentity.for_assistant_turn(
                agent_id=state.agent_id,
                user_id=state.user_id,
                session_id=state.session_id,
                run_id=state.run_id,
            )
            checkpoints = list(
                runtime.checkpointer.list(
                    {"configurable": {"thread_id": identity.thread_id}}
                )
            )
            checkpoint = next(
                saved
                for saved in checkpoints
                if saved.checkpoint["channel_values"].get("state").run_id
                == state.run_id
            )
            assert RUNTIME_STATE_KEYS.isdisjoint(checkpoint.checkpoint["channel_values"])
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
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=MemorySaver(),
    )
    try:
        runtime.run_state(_request("first"), run_id="run-one")
        runtime.run_state(_request("second"), run_id="run-two")

        assert [message["role"] for message in adapter.requests[2].messages] == [
            "system",
            "user",
        ]
    finally:
        runtime.close()
