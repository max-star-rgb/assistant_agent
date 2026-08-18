from __future__ import annotations

import asyncio
import json

import pytest
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, ToolRuntime

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.native_boundary import builtin_tool_metadata


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


def _create_probe_tool() -> BaseTool:
    @tool("probe", response_format="content_and_artifact")
    def probe(
        value: str,
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, str]], dict[str, str]]:
        """Return a core-contract probe result."""

        user_id = authenticated_user_identity(runtime)
        return (
            [{"type": "text", "text": json.dumps({"status": "ok"})}],
            {"value": value, "user_id": user_id},
        )

    probe.metadata = builtin_tool_metadata("read")
    return probe


@pytest.mark.core_invariant("TOOL-001")
def test_native_tool_schema_hides_runtime_owned_arguments() -> None:
    tool = _create_probe_tool()

    assert set(tool.tool_call_schema.model_fields) == {"value"}


@pytest.mark.core_invariant("TOOL-001")
def test_toolnode_injects_identity_and_returns_standard_tool_message() -> None:
    tool = _create_probe_tool()
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "probe",
                                "args": {"value": "value-sentinel"},
                                "id": "call-sentinel",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "assistant_id": "assistant-sentinel",
                    "graph_id": "graph-sentinel",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.content == [{"type": "text", "text": json.dumps({"status": "ok"})}]
    assert message.artifact == {"value": "value-sentinel", "user_id": "user-sentinel"}
