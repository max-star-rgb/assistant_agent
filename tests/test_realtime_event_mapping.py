import pytest

from assistant_agent.realtime.chunking import chunk_response_text
from assistant_agent.realtime.event_mapping import (
    map_agent_event,
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


def test_unsupported_agent_event_returns_no_realtime_event() -> None:
    event = AgentEvent(
        type="tool_progress",
        session_id="session-1",
        run_id="run-1",
        tool_name="product_search",
        progress=0.5,
    )

    assert map_agent_event(event) is None
    assert map_agent_event_with_final_response_chunks(event) == []


def test_chunk_response_text_bounds_long_text_without_token_streaming_semantics() -> None:
    chunks = chunk_response_text("One two three four five", max_chars=9)

    assert chunks == ["One two", "three", "four five"]
    assert all(len(chunk) <= 9 for chunk in chunks)
