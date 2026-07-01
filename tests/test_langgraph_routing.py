from assistant_agent.agent.conditional_graph import (
    build_conditional_agent_graph,
    route_by_intent,
    run_conditional_agent_graph,
)
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.workflow import AgentWorkflow
from assistant_agent.schemas.planning import IntentResult
from assistant_agent.schemas.requests import UserRequest


def graph_state_for_intent(intent_name: str) -> dict:
    request = UserRequest(user_id="u1", session_id="s1", text="test")
    state = AgentState.from_request(request)
    state.set_intent(
        IntentResult(
            intent=intent_name,
            confidence=0.9,
            rationale="test",
        )
    )
    return {
        "request": request,
        "state": state,
        "workflow": AgentWorkflow(),
        "outputs_by_step": {},
    }


def test_conditional_graph_can_compile() -> None:
    assert build_conditional_agent_graph() is not None


def test_search_intent_routes_to_search_node() -> None:
    assert route_by_intent(graph_state_for_intent("search_product")) == "search_node"


def test_image_generation_intent_routes_to_image_node() -> None:
    assert route_by_intent(graph_state_for_intent("generate_image")) == "image_generation_node"


def test_render_intent_routes_to_render_node() -> None:
    assert route_by_intent(graph_state_for_intent("render_3d")) == "render_node"


def test_unknown_or_chat_intent_does_not_crash() -> None:
    state = run_conditional_agent_graph(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="这个风格怎么样",
        )
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert state.response is not None


def test_conditional_graph_executes_search_node() -> None:
    state = run_conditional_agent_graph(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="找相似款",
        )
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "product_search"
    assert state.tool_calls[0].tool_name == "product_search"
