from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentError
from assistant_agent.agent.workflow import AgentWorkflow
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.services.trace_store import InMemoryTraceStore


class CapturingGraph:
    def __init__(self) -> None:
        self.config = None

    def invoke(self, initial_state, config=None):
        self.config = config
        initial_state["state"].set_response(AgentResponse(message="ok", data={}))
        return initial_state


class FailingGraph:
    def invoke(self, initial_state, config=None):
        state = initial_state["state"]
        state.errors.append(
            AgentError(
                message="provider network failure " + ("x" * 500),
                source="chat_provider",
                details={"code": "provider_network_error", "recovery_action": "retry"},
            )
        )
        state.status = "failed"
        return initial_state


def test_agent_graph_runtime_run_returns_agent_response() -> None:
    response = AgentGraphRuntime().run(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    assert isinstance(response, AgentResponse)
    assert response.data["intent"] == "product_search"
    assert response.data["tool_count"] == 1


def test_agent_graph_runtime_passes_run_id_as_langgraph_thread_id() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig(langgraph_checkpointer_backend="none"))
    graph = CapturingGraph()

    def select_graph(request, *, runtime_context=None):
        return graph

    runtime._select_graph = select_graph

    state = runtime.run_state(UserRequest(user_id="u1", session_id="thread_1", text="你好"))

    assert graph.config == {
        "configurable": {
            "thread_id": state.run_id,
            "session_id": "thread_1",
            "user_id": "u1",
            "run_id": state.run_id,
        }
    }
    session = runtime.session_store.get("u1", "thread_1")
    assert session is not None
    assert session.thread_id == "thread_1"
    assert session.last_run_id == state.run_id


def test_agent_graph_runtime_emits_prompt_safe_turn_summary() -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="private user text should stay out of summary",
            metadata={"source": "assistant_run_service"},
        )
    )

    summary_events = [
        event
        for event in trace_store.events
        if event.canonical_event == "assistant.turn.summary"
    ]
    assert len(summary_events) == 1
    event = summary_events[0]
    summary = event.output_summary["turn_summary"]
    assert summary["schema_version"] == "assistant_turn_summary_v1"
    assert summary["terminal_status"] == "completed"
    assert summary["response_present"] is True
    assert summary["client_type"] == "cli"
    assert summary["assistant_run_id"] == state.run_id
    assert summary["trace_id"] == state.trace_id
    assert summary["user_id"] == "u1"
    assert summary["session_id"] == "s1"
    dumped = event.model_dump_json()
    assert "private user text should stay out of summary" not in dumped
    assert state.response is not None
    assert state.response.message not in dumped


def test_agent_graph_runtime_failed_turn_summary_has_bounded_failure() -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)
    runtime._select_graph = lambda request, runtime_context=None: FailingGraph()

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="private failure request",
            metadata={"source": "assistant_run_service"},
        )
    )

    summary = next(
        event.output_summary["turn_summary"]
        for event in trace_store.events
        if event.canonical_event == "assistant.turn.summary"
    )
    assert state.status == "failed"
    assert summary["terminal_status"] == "failed"
    assert summary["response_present"] is False
    assert summary["error_count"] == 1
    assert summary["failure_summary"]["code"] == "provider_network_error"
    assert summary["failure_summary"]["source"] == "chat_provider"
    assert len(summary["failure_summary"]["message"]) <= 240
    dumped = str(summary)
    assert "private failure request" not in dumped


def test_agent_workflow_run_uses_graph_runtime_compatibly() -> None:
    state = AgentWorkflow().run(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "product_search"
    assert state.tool_calls[0].tool_name == "shopping_search"


def test_agent_graph_runtime_handles_image_generation_path() -> None:
    state = AgentGraphRuntime().run_state(
        UserRequest(user_id="u1", session_id="s1", text="生成一张日系海报")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "image_generation"
    assert state.tool_calls[0].tool_name == "image_generation"
    assert state.response is not None
    assert state.response.data["image_url"] == "local://generated/poster.png"


def test_agent_graph_runtime_handles_render_path() -> None:
    state = AgentGraphRuntime().run_state(
        UserRequest(user_id="u1", session_id="s1", text="渲染到客厅场景")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "render_3d"
    assert state.tool_calls[0].tool_name == "render_3d"
