from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import ListEventSink


def test_runtime_emits_ordered_task_graph_tool_and_final_events() -> None:
    sink = ListEventSink()

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    assert state.status == "completed"
    event_types = [event.type for event in sink.events]
    assert event_types[:2] == ["task_started", "graph_node_started"]
    assert "agent_trace_decision" in event_types
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert "agent_trace_observation" in event_types
    assert "agent_trace_final_answer" in event_types
    assert event_types[-3:] == ["graph_node_finished", "response_delta", "final_response"]
    tool_started = next(event for event in sink.events if event.type == "tool_started")
    tool_finished = next(event for event in sink.events if event.type == "tool_finished")
    response_delta = next(event for event in sink.events if event.type == "response_delta")
    assert tool_started.tool_name == "product_search"
    assert tool_finished.output_ref == "mock://products/white-low-top-sneaker"
    assert response_delta.payload["source"] == "runtime_final_response"
    assert response_delta.payload["token_streaming"] is False
    assert sink.events[-1].text


def test_runtime_emits_tool_failed_and_task_failed_events() -> None:
    sink = ListEventSink()

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="哪个便宜")
    )

    assert state.status == "failed"
    event_types = [event.type for event in sink.events]
    assert event_types[:2] == ["task_started", "graph_node_started"]
    assert "agent_trace_decision" in event_types
    assert "tool_started" in event_types
    assert "tool_failed" in event_types
    assert "agent_trace_observation" in event_types
    assert "agent_trace_final_answer" in event_types
    assert event_types[-2:] == ["graph_node_finished", "task_failed"]
    tool_failed = next(event for event in sink.events if event.type == "tool_failed")
    assert tool_failed.tool_name == "price_compare"
    assert tool_failed.error
    assert sink.events[-1].error


def test_runtime_emits_response_delta_for_direct_chat_stream() -> None:
    sink = ListEventSink()

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="你好")
    )

    assert state.status == "completed"
    assert state.response is not None
    deltas = [event for event in sink.events if event.type == "response_delta"]
    assert deltas
    assert "".join(event.text or "" for event in deltas) in state.response.message
    assert deltas[0].payload["source"] == "direct_chat"
    assert deltas[0].payload["token_streaming"] is False
