import pytest

from assistant_agent.realtime.chunking import chunk_response_text
from assistant_agent.realtime.event_mapping import (
    map_agent_event,
    map_agent_event_stream,
    map_agent_event_with_final_response_chunks,
)
from assistant_agent.schemas.events import AgentEvent


@pytest.mark.parametrize(
    ("agent_type", "realtime_type"),
    [
        ("tool_started", "tool.started"),
        ("tool_finished", "tool.finished"),
        ("tool_completed", "tool.finished"),
        ("tool_failed", "tool.failed"),
    ],
)
def test_maps_tool_lifecycle_events(agent_type: str, realtime_type: str) -> None:
    event = AgentEvent(
        type=agent_type,
        session_id="session-1",
        run_id="run-1",
        tool_name="product_search",
        output_ref="mock://result",
        error={"code": "TOOL_FAILED", "message": "tool failed"},
        payload={"call_id": "call-1", "step_id": "step-1"},
    )

    mapped = map_agent_event(event)

    assert mapped is not None
    assert mapped.type == realtime_type
    assert mapped.display_only is True
    assert mapped.payload["agent_event_type"] == agent_type
    assert mapped.payload["session_id"] == "session-1"
    assert mapped.payload["run_id"] == "run-1"
    assert mapped.payload["tool_name"] == "product_search"
    assert mapped.payload["output_ref"] == "mock://result"
    assert mapped.payload["call_id"] == "call-1"
    assert mapped.payload["step_id"] == "step-1"
    assert mapped.payload["error"]["message"] == "tool failed"


@pytest.mark.parametrize(
    ("agent_type", "realtime_type"),
    [
        ("agent_trace_decision", "trace.decision"),
        ("agent_trace_observation", "trace.observation"),
    ],
)
def test_maps_agent_trace_events(agent_type: str, realtime_type: str) -> None:
    trace = {"event": "decision", "action": "product_search", "iteration": 1}
    event = AgentEvent(
        type=agent_type,
        session_id="session-1",
        run_id="run-1",
        tool_name="product_search",
        payload={"decision_trace": trace},
    )

    mapped = map_agent_event(event)

    assert mapped is not None
    assert mapped.type == realtime_type
    assert mapped.display_only is True
    assert mapped.payload["decision_trace"] == trace
    assert mapped.payload["tool_name"] == "product_search"


def test_maps_final_response_to_final_event() -> None:
    event = AgentEvent(
        type="final_response",
        session_id="session-1",
        run_id="run-1",
        text="The final answer.",
    )

    mapped = map_agent_event(event)

    assert mapped is not None
    assert mapped.type == "response.final"
    assert mapped.text == "The final answer."
    assert mapped.display_only is False
    assert mapped.payload["agent_event_type"] == "final_response"


def test_maps_response_delta_to_response_chunk() -> None:
    event = AgentEvent(
        type="response_delta",
        session_id="session-1",
        run_id="run-1",
        text="partial",
        payload={"token_streaming": True, "source": "direct_chat"},
    )

    mapped = map_agent_event(event)

    assert mapped is not None
    assert mapped.type == "response.chunk"
    assert mapped.text == "partial"
    assert mapped.display_only is False
    assert mapped.payload["agent_event_type"] == "response_delta"
    assert mapped.payload["token_streaming"] is True
    assert mapped.payload["source"] == "direct_chat"


@pytest.mark.parametrize(
    "agent_type",
    ["tts_started", "tts_finished", "tts_superseded", "display_superseded", "call_hangup"],
)
def test_tts_and_media_lifecycle_events_do_not_stream_as_realtime_frames(agent_type: str) -> None:
    event = AgentEvent(
        type=agent_type,
        session_id="session-1",
        run_id="run-1",
        payload={"event_id": "evt-lifecycle"},
    )

    assert map_agent_event_stream(event) == []


def test_maps_task_started_to_progress_stream_event() -> None:
    event = AgentEvent(
        type="task_started",
        session_id="session-1",
        run_id="run-1",
        payload={"user_id": "user-1"},
    )

    mapped = map_agent_event_stream(event)

    assert [item.type for item in mapped] == ["run.progress"]
    assert mapped[0].display_only is True
    assert mapped[0].payload["stage"] == "task"
    assert mapped[0].payload["status"] == "started"
    assert mapped[0].payload["next_step"] == "run_assistant_workflow"
    assert mapped[0].text == "Started processing the request."


def test_tool_started_stream_includes_progress_and_tool_lifecycle_event() -> None:
    event = AgentEvent(
        type="tool_started",
        session_id="session-1",
        run_id="run-1",
        tool_name="product_search",
        payload={"call_id": "call-1", "step_id": "step-1"},
    )

    mapped = map_agent_event_stream(event)

    assert [item.type for item in mapped] == ["run.progress", "tool.started"]
    progress = mapped[0]
    assert progress.text == "Calling product_search."
    assert progress.payload["stage"] == "tool"
    assert progress.payload["status"] == "working"
    assert progress.payload["current_step"] == "step-1"
    assert mapped[1].payload["tool_name"] == "product_search"


def test_pending_confirmation_maps_to_confirmation_event_without_completed_progress() -> None:
    event = AgentEvent(
        type="tool_finished",
        session_id="session-1",
        run_id="run-1",
        tool_name="calendar.create_event",
        payload={
            "call_id": "call-1",
            "step_id": "step-1",
            "post_tool_call": {
                "status": "pending_confirmation",
                "confirmation": {
                    "required": True,
                    "id": "confirm-1",
                    "kind": "external_write",
                },
            },
        },
    )

    mapped = map_agent_event_stream(event)

    assert [item.type for item in mapped] == ["tool.finished", "confirmation.required"]
    confirmation = mapped[-1]
    assert confirmation.text == "Please confirm before I run calendar.create_event."
    assert confirmation.payload["tool_name"] == "calendar.create_event"
    assert confirmation.payload["confirmation_id"] == "confirm-1"
    assert confirmation.payload["confirmation_kind"] == "external_write"


def test_progress_message_streams_as_replaceable_run_progress_only() -> None:
    event = AgentEvent(
        type="progress_message",
        session_id="session-1",
        run_id="run-1",
        tool_name="product_search",
        text="我查一下。",
        payload={"replaceable": True, "source": "native_tool_wait"},
    )

    mapped = map_agent_event_stream(event)

    assert [item.type for item in mapped] == ["run.progress"]
    assert mapped[0].text == "我查一下。"
    assert mapped[0].display_only is True
    assert mapped[0].payload["agent_event_type"] == "progress_message"
    assert mapped[0].payload["stage"] == "tool"
    assert mapped[0].payload["status"] == "working"
    assert mapped[0].payload["tool_name"] == "product_search"
    assert mapped[0].payload["replaceable"] is True


def test_final_response_mapping_emits_text_chunks_before_final() -> None:
    event = AgentEvent(
        type="final_response",
        session_id="session-1",
        run_id="run-1",
        text="Alpha beta gamma delta.",
    )

    mapped = map_agent_event_with_final_response_chunks(event, max_chunk_chars=12)

    assert [item.type for item in mapped] == ["response.chunk", "response.chunk", "response.final"]
    assert [item.text for item in mapped] == [
        "Alpha beta",
        "gamma delta.",
        "Alpha beta gamma delta.",
    ]
    assert mapped[0].payload["chunk_index"] == 0
    assert mapped[0].payload["chunk_count"] == 2
    assert mapped[0].payload["chunking_strategy"] == "bounded_final_text"
    assert mapped[0].payload["token_streaming"] is False
    assert mapped[-1].payload["agent_event_type"] == "final_response"


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_final_response_text_does_not_emit_chunks(text: str | None) -> None:
    event = AgentEvent(type="final_response", session_id="session-1", run_id="run-1", text=text)

    mapped = map_agent_event_with_final_response_chunks(event)

    assert [item.type for item in mapped] == ["response.final"]
    assert mapped[0].text == text


@pytest.mark.parametrize(
    ("agent_type", "error", "expected_text"),
    [
        ("agent_error", {"code": "ACCESS_DENIED", "message": "access denied"}, "access denied"),
        ("task_failed", "run failed", "run failed"),
    ],
)
def test_maps_agent_error_events(agent_type: str, error: str | dict, expected_text: str) -> None:
    event = AgentEvent(type=agent_type, session_id="session-1", run_id="run-1", error=error)

    mapped = map_agent_event(event)

    assert mapped is not None
    assert mapped.type == "error"
    assert mapped.text == expected_text
    assert mapped.payload["agent_event_type"] == agent_type
    assert mapped.payload["error"] == error


def test_tool_progress_maps_to_progress_stream_event() -> None:
    event = AgentEvent(
        type="tool_progress",
        session_id="session-1",
        run_id="run-1",
        tool_name="product_search",
        progress=0.5,
    )

    assert map_agent_event(event) is None
    mapped = map_agent_event_stream(event)
    assert [item.type for item in mapped] == ["run.progress"]
    assert mapped[0].payload["tool_name"] == "product_search"
    assert mapped[0].payload["progress"] == 0.5
    assert mapped[0].payload["stage"] == "tool"
    assert mapped[0].payload["status"] == "working"


def test_tool_progress_preserves_explicit_task_state_stage_and_strategy() -> None:
    event = AgentEvent(
        type="tool_progress",
        session_id="session-1",
        run_id="run-1",
        tool_name="task_state",
        text="Using previous findings to revise the task.",
        payload={
            "stage": "task_state",
            "status": "revising",
            "current_step": "intent_revision",
            "strategy": "reuse_and_replan",
            "reusable_artifact_count": 1,
        },
    )

    mapped = map_agent_event_stream(event)

    assert [item.type for item in mapped] == ["run.progress"]
    assert mapped[0].text == "Using previous findings to revise the task."
    assert mapped[0].payload["stage"] == "task_state"
    assert mapped[0].payload["status"] == "revising"
    assert mapped[0].payload["current_step"] == "intent_revision"
    assert mapped[0].payload["strategy"] == "reuse_and_replan"
    assert mapped[0].payload["reusable_artifact_count"] == 1


def test_chunk_response_text_bounds_long_text_without_token_streaming_semantics() -> None:
    chunks = chunk_response_text("One two three four five", max_chars=9)

    assert chunks == ["One two", "three", "four five"]
    assert all(len(chunk) <= 9 for chunk in chunks)
