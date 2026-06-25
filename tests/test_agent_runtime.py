from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.requests import AgentResponse, UserRequest


class CapturingGraph:
    def __init__(self) -> None:
        self.config = None

    def invoke(self, initial_state, config=None):
        self.config = config
        initial_state["state"].set_response(AgentResponse(message="ok", data={}))
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


def test_agent_workflow_run_uses_graph_runtime_compatibly() -> None:
    state = AgentWorkflow().run(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "product_search"
    assert state.tool_calls[0].tool_name == "product_search"


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
