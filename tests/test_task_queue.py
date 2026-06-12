from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.task_queue import AgentTask, InMemoryTaskQueue, InlineTaskQueue


def make_task(text: str = "帮我找相似款") -> AgentTask:
    request = UserRequest(user_id="u1", session_id="s1", text=text)
    return AgentTask(user_id=request.user_id, session_id=request.session_id, request=request)


def test_inline_task_queue_submit_runs_task_successfully() -> None:
    queue = InlineTaskQueue()

    handle = queue.submit(make_task())

    assert handle.task_id.startswith("task_")
    assert handle.status == "success"
    assert queue.get_status(handle.task_id) == "success"


def test_in_memory_task_queue_returns_runtime_events() -> None:
    queue = InMemoryTaskQueue()
    handle = queue.submit(make_task())

    events = queue.get_events(handle.task_id)

    assert [event.type for event in events] == [
        "task_started",
        "graph_node_started",
        "tool_started",
        "tool_finished",
        "graph_node_finished",
        "final_response",
    ]
    assert events[2].tool_name == "product_search"


def test_task_queue_records_failed_task_status_and_events() -> None:
    queue = InMemoryTaskQueue()

    handle = queue.submit(make_task("哪个便宜"))

    assert handle.status == "failed"
    assert queue.get_status(handle.task_id) == "failed"
    event_types = [event.type for event in queue.get_events(handle.task_id)]
    assert "tool_failed" in event_types
    assert event_types[-1] == "task_failed"


def test_get_events_for_unknown_task_returns_empty_list() -> None:
    queue = InlineTaskQueue()

    assert queue.get_events("missing") == []
