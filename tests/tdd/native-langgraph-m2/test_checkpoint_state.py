from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.runtime.assistant_graph_app import GraphExecutionIdentity
from assistant_agent.runtime.assistant_graph_state import (
    ASSISTANT_GRAPH_NAME,
    ASSISTANT_GRAPH_VERSION,
    ASSISTANT_STATE_SCHEMA_VERSION,
    AssistantStateCompatibilityError,
    assistant_turn_state_from_agent_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from tests.core.support import ProbeTool, ScriptedChatAdapter, offline_config, sealed_registry


_FORBIDDEN_TYPES = (
    AgentState,
    UserRequest,
    Path,
    bytes,
    bytearray,
)


def _walk(value: Any) -> None:
    assert not isinstance(value, _FORBIDDEN_TYPES), type(value).__name__
    module = type(value).__module__
    name = type(value).__name__
    assert not (
        module.startswith("assistant_agent")
        and name in {"ToolResult", "ToolExecutor", "ToolRegistry"}
    ), f"forbidden checkpoint object: {module}.{name}"
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item)


def _runtime(*, saver: InMemorySaver, adapter: ScriptedChatAdapter) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )


def _identity(run_id: str) -> GraphExecutionIdentity:
    return GraphExecutionIdentity.for_assistant_turn(
        agent_id="assistant",
        user_id="user-state",
        session_id="session-state",
        run_id=run_id,
    )


def test_real_compiled_checkpoint_contains_only_plain_json_state() -> None:
    """Returning legacy runtime models from any node must make this checkpoint audit fail."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter([
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="done-state",
            )
        ]),
    )
    try:
        result = runtime.run_state(
            UserRequest(
                user_id="user-state",
                session_id="session-state",
                text="checkpoint me",
                image_ids=["image-ref-1"],
                video_ids=["video-ref-1"],
                audio_id="audio-ref-1",
                metadata={"artifact_body": b"must-not-persist"},
            ),
            run_id="run-state-1",
        )
        assert result.status == "completed"
        snapshot = runtime.assistant_graph_app.graph.get_state(
            _identity("run-state-1").runnable_config()
        )
        values = snapshot.values
        _walk(values)
        json.dumps(values)
        assert values["graph_name"] == ASSISTANT_GRAPH_NAME
        assert values["graph_version"] == ASSISTANT_GRAPH_VERSION
        assert values["state_schema_version"] == ASSISTANT_STATE_SCHEMA_VERSION
        assert values["request"]["media_refs"] == [
            {"kind": "image", "ref": "image-ref-1"},
            {"kind": "video", "ref": "video-ref-1"},
            {"kind": "audio", "ref": "audio-ref-1"},
        ]
        assert "artifact_body" not in json.dumps(values)
    finally:
        runtime.close()


def test_incompatible_state_version_fails_closed() -> None:
    """Accepting an unknown schema version would silently resume with wrong semantics."""

    state = AgentState.from_request(
        UserRequest(user_id="u", session_id="s", text="hello"),
        run_id="run-version",
        trace_id="trace-version",
    )
    persisted = assistant_turn_state_from_agent_state(state)
    persisted["state_schema_version"] = 999

    with pytest.raises(AssistantStateCompatibilityError) as exc_info:
        validate_assistant_turn_state(persisted)

    assert exc_info.value.code == "assistant_state_version_incompatible"


def test_stable_thread_new_turn_overwrites_all_run_scoped_channels() -> None:
    """LangGraph's checkpoint merge must not leak the prior turn trajectory."""

    saver = InMemorySaver()
    runtime = _runtime(
        saver=saver,
        adapter=ScriptedChatAdapter([
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[NativeToolCall(
                    id="provider-call-1",
                    name=ProbeTool.name,
                    arguments={"value": "first"},
                )],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="first-finished",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="second-finished",
            ),
        ]),
    )
    try:
        first = runtime.run_state(
            UserRequest(user_id="user-state", session_id="session-state", text="first"),
            run_id="run-state-1",
        )
        assert first.tool_results
        second = runtime.run_state(
            UserRequest(user_id="user-state", session_id="session-state", text="second"),
            run_id="run-state-2",
        )
        assert second.response is not None
        assert second.response.message == "second-finished"
        values = runtime.assistant_graph_app.graph.get_state(
            _identity("run-state-2").runnable_config()
        ).values
        assert values["run"]["run_id"] == "run-state-2"
        assert values["run"]["errors"] == []
        assert values["run"]["tool_calls"] == []
        assert values["run"]["tool_results"] == []
        assert values["outputs_by_step"] == []
        assert values["pending_tool_calls"] == []
        assert values["tool_observations"] == []
        assert values["assistant_iterations"] == 1
        assert values["tool_calls_used"] == 0
        assert values["action_tool_calls_used"] == 0
        assert values["control_tool_calls_used"] == 0
        assert values["pending_interrupt"] is None
        assert values["final_response"]["message"] == "second-finished"
    finally:
        runtime.close()
