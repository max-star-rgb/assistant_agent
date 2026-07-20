from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.realtime import RealtimeAgentEvent


def test_realtime_progress_event_maps_to_gateway_progress_frame() -> None:
    event = RealtimeAgentEvent(
        type="run.progress",
        text="Calling shopping_search.",
        payload={
            "stage": "tool",
            "status": "working",
            "current_step": "shopping_search",
            "progress": 0.25,
        },
        display_only=True,
    )

    mapped = realtime_event_to_frame(
        event,
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert mapped is not None
    assert mapped["type"] == "event.progress"
    assert mapped["session_id"] == "session-1"
    assert mapped["turn_id"] == "turn-1"
    assert mapped["run_id"] == "run-1"
    assert mapped["payload"]["text"] == "Calling shopping_search."
    assert mapped["payload"]["message"] == "Calling shopping_search."
    assert mapped["payload"]["stage"] == "tool"
    assert mapped["payload"]["status"] == "working"
    assert mapped["payload"]["progress"] == 0.25
    assert mapped["payload"]["display_only"] is True
    assert mapped["payload"]["speech_policy"] == "optional"
    assert mapped["payload"]["persistence"] == "ephemeral"
    assert mapped["payload"]["replaceable"] is True
    assert mapped["payload"]["replacement_key"] == "run-1:progress"


def test_response_chunk_supersedes_progress_and_is_final_content() -> None:
    mapped = realtime_event_to_frame(
        RealtimeAgentEvent(type="response.chunk", text="明天上午十点开会。"),
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert mapped is not None
    assert mapped["type"] == "stream.chunk"
    assert mapped["payload"]["speech_policy"] == "required"
    assert mapped["payload"]["persistence"] == "final"
    assert mapped["payload"]["replaceable"] is False
    assert mapped["payload"]["supersedes"] == ["run-1:progress"]


def test_tool_lifecycle_event_is_ephemeral_and_not_speakable() -> None:
    mapped = realtime_event_to_frame(
        RealtimeAgentEvent(type="tool.started", payload={"tool_name": "calendar.search"}),
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert mapped is not None
    assert mapped["type"] == "event.tool"
    assert mapped["payload"]["speech_policy"] == "never"
    assert mapped["payload"]["persistence"] == "ephemeral"
    assert mapped["payload"]["replaceable"] is False


def test_confirmation_required_is_speakable_but_not_final_content() -> None:
    mapped = realtime_event_to_frame(
        RealtimeAgentEvent(
            type="confirmation.required",
            text="要为你创建这个日历事件吗？",
            payload={"confirmation_id": "confirm-1"},
        ),
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
    )

    assert mapped is not None
    assert mapped["type"] == "confirmation.required"
    assert mapped["payload"]["text"] == "要为你创建这个日历事件吗？"
    assert mapped["payload"]["expects_reply"] is True
    assert mapped["payload"]["speech_policy"] == "required"
    assert mapped["payload"]["persistence"] == "ephemeral"
    assert mapped["payload"]["replaceable"] is False
