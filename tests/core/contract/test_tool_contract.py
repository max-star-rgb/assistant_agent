from __future__ import annotations

import asyncio
import json

from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, ConfigDict
import pytest

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.models import ToolResult


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str
    user_id: str


class _ProbeTool(ToolBase):
    name = "probe"
    description = "probe"
    input_schema = _Input
    output_schema = _Input
    category = "read"
    runtime_input_bindings = (
        RuntimeInputBinding(field="user_id", source="runtime_identity", key="user_id"),
    )

    def _execute(self, input: _Input, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value, "user_id": input.user_id},
            model_observation={"status": "ok"},
        )


@pytest.mark.core_invariant("TOOL-001")
def test_native_tool_schema_hides_runtime_owned_arguments() -> None:
    tool = _ProbeTool()

    assert set(tool.tool_call_schema.model_fields) == {"value"}
    assert set(tool.args_schema.model_fields) == {"value", "runtime"}


@pytest.mark.core_invariant("TOOL-001")
def test_toolnode_injects_identity_and_returns_standard_tool_message() -> None:
    tool = _ProbeTool()
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
            context=AssistantRunContext(
                user_id="user-sentinel",
                tenant_id="tenant-sentinel",
            ),
        )
    )

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert json.loads(message.content) == {"status": "ok"}
    assert message.artifact == {"value": "value-sentinel", "user_id": "user-sentinel"}
