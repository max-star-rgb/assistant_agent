from multimodal_agent.agent.graph import build_agent_graph, run_agent_graph
from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.schemas.requests import UserRequest


def test_langgraph_workflow_can_compile() -> None:
    graph = build_agent_graph()

    assert graph is not None


def test_langgraph_workflow_handles_simple_query() -> None:
    state = run_agent_graph(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="图里是什么",
            image_ids=["img1"],
        )
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "understand_image"
    assert state.response is not None
    assert state.tool_calls[0].tool_name == "vision_understanding"


def test_agent_workflow_run_still_works() -> None:
    state = AgentWorkflow().run(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="找相似款",
        )
    )

    assert state.intent is not None
    assert state.intent.intent == "product_search"
    assert state.status == "completed"
    assert state.tool_calls[0].tool_name == "product_search"
