from __future__ import annotations

from langchain_core.tools import StructuredTool
import pytest

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import (
    build_fast_agent,
    render_minimal_system_prompt,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel


@pytest.mark.core_invariant("CTX-001")
def test_frozen_memory_is_rendered_as_untrusted_prompt_data() -> None:
    prompt = render_minimal_system_prompt(
        ("memory-sentinel",),
        AssistantRunContext(),
    )

    assert "memory_context_untrusted_v1" in prompt
    assert "memory-sentinel" in prompt


@pytest.mark.core_invariant("CTX-001")
def test_create_agent_owns_limits_summary_and_hitl_middleware() -> None:
    def write_probe(value: str) -> str:
        """probe"""

        return value

    tool = StructuredTool.from_function(
        write_probe,
        name="write_probe",
        metadata={"effect": "write"},
    )
    graph = build_fast_agent(MockAssistantChatModel(), [tool])
    nodes = set(graph.get_graph().nodes)

    assert any("ModelCallLimitMiddleware" in node for node in nodes)
    assert any("ToolCallLimitMiddleware" in node for node in nodes)
    assert any("SummarizationMiddleware" in node for node in nodes)
    assert any("HumanInTheLoopMiddleware" in node for node in nodes)
