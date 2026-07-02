import pytest

from assistant_agent.agent.cancellation import AgentRunCancelled
from assistant_agent.agent.graph_runtime import GraphRuntimeContext, bind_runtime_node
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.run_history import RunHistoryStore
from assistant_agent.services.session_store import InMemorySessionStore


class MutableCancelToken:
    def __init__(self, cancelled: bool = False, metadata: dict[str, object] | None = None) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


def test_runtime_pre_graph_cancel_marks_run_cancelled_without_final_response(tmp_path) -> None:
    sink = ListEventSink()
    run_history = RunHistoryStore(tmp_path / "runs.jsonl")
    session_store = InMemorySessionStore()

    state = AgentGraphRuntime(
        event_sink=sink,
        run_history=run_history,
        session_store=session_store,
    ).run_state(
        UserRequest(user_id="u1", session_id="s1", text="hello"),
        cancel_token=MutableCancelToken(cancelled=True),
    )

    assert state.status == "cancelled"
    assert state.response is None
    assert state.errors[-1].details["cancel_phase"] == "pre_graph"
    assert [record.status for record in run_history.read_all()] == ["started", "cancelled"]
    assert session_store.get("u1", "s1").last_status == "cancelled"
    assert [event.type for event in sink.events] == ["task_started", "task_cancelled"]


def test_runtime_pre_graph_cancel_records_token_metadata() -> None:
    token = MutableCancelToken(
        cancelled=True,
        metadata={
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
            "deadline_ms": 100,
        },
    )

    state = AgentGraphRuntime().run_state(
        UserRequest(user_id="u1", session_id="s1", text="hello"),
        cancel_token=token,
    )

    assert state.status == "cancelled"
    assert state.errors[-1].details["cancel_phase"] == "pre_graph"
    assert state.errors[-1].details["cancel_source"] == "deadline"
    assert state.errors[-1].details["cancel_reason"] == "run_deadline_expired"
    assert state.errors[-1].details["deadline_ms"] == 100


def test_bound_graph_node_raises_after_node_cancel_with_latest_state() -> None:
    token = MutableCancelToken(cancelled=False)
    runtime = AgentGraphRuntime()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="hello"))
    context = GraphRuntimeContext(
        tool_executor=runtime.tool_executor,
        chat_adapter=runtime.chat_adapter,
        memory_manager=runtime.memory_manager,
        cancel_token=token,
    )

    def node(graph_state):
        token.cancelled = True
        return {**graph_state, "state": state}

    wrapped = bind_runtime_node("probe_node", node, runtime_context=context, trace=False)

    with pytest.raises(AgentRunCancelled) as exc_info:
        wrapped({"state": state})

    assert exc_info.value.phase == "after_node"
    assert exc_info.value.node_name == "probe_node"
    assert exc_info.value.state is state
