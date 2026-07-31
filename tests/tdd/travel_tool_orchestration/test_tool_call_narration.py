from assistant_agent.runtime.assistant_loop_nodes import (
    _native_tool_call_decisions,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.output_models import NativeToolCall


def test_provider_text_on_tool_call_turn_is_not_exposed_as_progress() -> None:
    result = ChatResult(
        provider="test",
        response_text="我先读取可用项目 Skill，再为你搜索酒店。",
        tool_calls=[
            NativeToolCall(
                id="call-1",
                name="load_skill",
                arguments={"skill_id": "travel-tool-orchestration"},
            )
        ],
    )

    decisions = _native_tool_call_decisions(result)

    assert len(decisions) == 1
    assert decisions[0].tool_name == "load_skill"
    assert decisions[0].progress_message is None
