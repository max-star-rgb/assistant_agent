from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.event_sink import ListEventSink


def test_runtime_emits_ordered_task_graph_tool_and_final_events() -> None:
    sink = ListEventSink()

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    assert state.status == "completed"
    assert [event.type for event in sink.events] == [
        "task_started",
        "graph_node_started",
        "tool_started",
        "tool_finished",
        "graph_node_finished",
        "final_response",
    ]
    assert sink.events[2].tool_name == "product_search"
    assert sink.events[3].output_ref == "mock://products/white-low-top-sneaker"
    assert sink.events[-1].text


def test_runtime_emits_tool_failed_and_task_failed_events() -> None:
    sink = ListEventSink()

    state = AgentGraphRuntime(event_sink=sink).run_state(
        UserRequest(user_id="u1", session_id="s1", text="哪个便宜")
    )

    assert state.status == "failed"
    assert [event.type for event in sink.events] == [
        "task_started",
        "graph_node_started",
        "tool_started",
        "tool_failed",
        "graph_node_finished",
        "task_failed",
    ]
    assert sink.events[3].tool_name == "price_compare"
    assert sink.events[3].error
    assert sink.events[-1].error
