from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.skills.loading import SkillCatalog


class _RepeatLimitedToolsModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        tool_messages = [
            message for message in messages if isinstance(message, ToolMessage)
        ]
        if len(tool_messages) >= 4:
            return AIMessage(content="terminal-answer")
        attempt = len(tool_messages) // 2 + 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "limited_alpha",
                    "args": {"value": "alpha"},
                    "id": f"call-alpha-{attempt}",
                    "type": "tool_call",
                },
                {
                    "name": "limited_beta",
                    "args": {"value": "beta"},
                    "id": f"call-beta-{attempt}",
                    "type": "tool_call",
                },
            ],
        )


def test_one_generic_node_enforces_metadata_limits_for_multiple_tools() -> None:
    executions = {"limited_alpha": 0, "limited_beta": 0}

    def run_alpha(value: str) -> str:
        """Run the alpha probe."""

        executions["limited_alpha"] += 1
        return value

    def run_beta(value: str) -> str:
        """Run the beta probe."""

        executions["limited_beta"] += 1
        return value

    tools = [
        StructuredTool.from_function(
            run_alpha,
            name="limited_alpha",
            metadata={"effect": "generate", "run_call_limit": 1},
        ),
        StructuredTool.from_function(
            run_beta,
            name="limited_beta",
            metadata={"effect": "generate", "run_call_limit": 1},
        ),
    ]
    graph = build_fast_agent(
        _RepeatLimitedToolsModel(),
        tools,
        skill_catalog=SkillCatalog(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="exercise-limits")],
                "execution_mode": "fast",
            },
            context=AssistantRunContext(),
        )
    )

    limiter_nodes = [
        name
        for name in graph.get_graph().nodes
        if name.startswith("PerToolCallLimitMiddleware")
    ]
    limit_errors = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.status == "error"
    ]
    assert limiter_nodes == ["PerToolCallLimitMiddleware.after_model"]
    assert executions == {"limited_alpha": 1, "limited_beta": 1}
    assert {message.name for message in limit_errors} == {
        "limited_alpha",
        "limited_beta",
    }
