from pathlib import Path

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.requests import UserRequest


def test_langgraph_loop_executes_multistep_task_one_step_at_a_time() -> None:
    state = AgentGraphRuntime().run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="找视频里的鞋子，比较价格，再生成海报",
            video_ids=["video_loop_1"],
        )
    )

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls[:4]] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert state.response is not None
    assert state.response.data["tool_count"] >= 4


def test_conditional_graph_uses_explicit_multistep_loop_nodes() -> None:
    source = Path("src/multimodal_agent/agent/conditional_graph.py").read_text()

    assert 'graph.add_node("select_next_step", select_next_step_node)' in source
    assert 'graph.add_node("execute_step", execute_step_node)' in source
    assert 'graph.add_conditional_edges(\n        "execute_step",' in source
    assert '"multi_tool_node": "plan_steps"' in source
