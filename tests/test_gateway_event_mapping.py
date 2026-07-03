from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.realtime import RealtimeAgentEvent


def test_realtime_progress_event_maps_to_gateway_progress_frame() -> None:
    event = RealtimeAgentEvent(
        type="run.progress",
        text="Calling product_search.",
        payload={
            "stage": "tool",
            "status": "working",
            "current_step": "product_search",
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
    assert mapped["payload"]["text"] == "Calling product_search."
    assert mapped["payload"]["message"] == "Calling product_search."
    assert mapped["payload"]["stage"] == "tool"
    assert mapped["payload"]["status"] == "working"
    assert mapped["payload"]["progress"] == 0.25
    assert mapped["payload"]["display_only"] is True
