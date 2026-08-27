"""Minimal native ToolNode harness for isolated local system evals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain.agents import AgentState
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
)


@dataclass(frozen=True)
class NativeToolInvocation:
    message: ToolMessage

    @property
    def artifact(self) -> dict[str, Any]:
        return dict(self.message.artifact or {})

    @property
    def status(self) -> str:
        return "succeeded" if self.message.status == "success" else "failed"


class _EvalUser:
    permissions: tuple[str, ...] = ()

    def __init__(self, identity: str) -> None:
        self.identity = identity


def invoke_native_tool(
    tool: BaseTool,
    arguments: dict[str, Any],
    *,
    user_identity: str,
    thread_id: str,
    tool_call_id: str,
    request_content: str | list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    run_context: AssistantRunContext | None = None,
) -> NativeToolInvocation:
    """Execute one real BaseTool through LangGraph's standard ToolNode."""

    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool], handle_tool_errors=False))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    messages = []
    if request_content is not None:
        messages.append(HumanMessage(content=request_content))
    messages.append(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool.name,
                    "args": arguments,
                    "id": tool_call_id,
                    "type": "tool_call",
                }
            ],
        )
    )
    result = graph.invoke(
        {
            **(state or {}),
            "messages": messages,
        },
        context=(run_context or AssistantRunContext()),
        config={
            "configurable": {
                "assistant_id": "system-eval-assistant",
                "graph_id": "system-eval-tool-graph",
                "thread_id": thread_id,
                "langgraph_auth_user": _EvalUser(user_identity),
            },
            "metadata": assistant_runtime_metadata(
                AssistantRuntimeFacts(entry_profile="system_eval")
            ),
        },
    )
    message = result["messages"][-1]
    if not isinstance(message, ToolMessage):
        raise RuntimeError("native ToolNode did not return a ToolMessage")
    return NativeToolInvocation(message=message)


__all__ = ["NativeToolInvocation", "invoke_native_tool"]
