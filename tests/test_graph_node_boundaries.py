from pathlib import Path

from multimodal_agent.agent.conditional_graph import run_conditional_agent_graph
from multimodal_agent.schemas.requests import UserRequest


PRIVATE_WORKFLOW_METHODS = ("._build_tool_input", "._run_tool", "._compose_response", "._save_demo_memory")


def test_graph_files_do_not_call_workflow_private_methods() -> None:
    for path in (
        Path("src/multimodal_agent/agent/graph.py"),
        Path("src/multimodal_agent/agent/conditional_graph.py"),
        Path("src/multimodal_agent/agent/graph_nodes.py"),
    ):
        source = path.read_text()
        assert all(method not in source for method in PRIVATE_WORKFLOW_METHODS)


def test_conditional_graph_still_executes_multi_tool_path_after_refactor() -> None:
    state = run_conditional_agent_graph(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找视频里的鞋子，比较价格，然后生成一张日系海报。",
            video_ids=["v1"],
        )
    )

    assert state.status == "completed"
    tool_names = [call.tool_name for call in state.tool_calls]
    assert "video_understanding" in tool_names
    assert "product_search" in tool_names
    assert "price_compare" in tool_names
    assert "image_generation" in tool_names
