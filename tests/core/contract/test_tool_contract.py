from __future__ import annotations

import asyncio
import json

import pytest
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, ToolRuntime

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.tools import native_boundary
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import (
    builtin_tool_metadata,
    invoke_native_tool,
)


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _SingleToolCallModel(MockAssistantChatModel):
    tool_name: str
    tool_args: dict[str, object]

    def _response_message(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="tool-result-observed")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.tool_name,
                    "args": self.tool_args,
                    "id": "call-error-sentinel",
                    "type": "tool_call",
                }
            ],
        )


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


@pytest.mark.core_invariant("TOOL-001")
def test_non_read_expected_failure_uses_default_toolnode_error_message() -> None:
    @tool("write_failure_probe", response_format="content_and_artifact")
    def write_failure_probe(value: str):
        """Return a deterministic expected write failure."""

        return invoke_native_tool(
            "write_failure_probe",
            lambda: ToolResult(
                tool_name="write_failure_probe",
                success=False,
                error=f"expected-write-failure:{value}",
            ),
        )

    configured = _configure_builtin_probe(write_failure_probe, "write")
    message = _invoke_default_toolnode(configured, {"value": "sentinel"})

    assert message.status == "error"
    assert message.content == "expected-write-failure:sentinel"


@pytest.mark.core_invariant("TOOL-001")
def test_non_read_unknown_failure_is_sanitized_by_default_toolnode() -> None:
    @tool("unknown_failure_probe", response_format="content_and_artifact")
    def unknown_failure_probe():
        """Raise one unexpected implementation failure."""

        def fail() -> ToolResult:
            raise RuntimeError(
                "api_key=secret-sentinel path=/home/private-sentinel/result.json"
            )

        return invoke_native_tool("unknown_failure_probe", fail)

    configured = _configure_builtin_probe(unknown_failure_probe, "write")
    message = _invoke_default_toolnode(configured, {})
    content = str(message.content)

    assert message.status == "error"
    assert "[redacted]" in content
    assert "secret-sentinel" not in content
    assert "/home/private-sentinel" not in content


@pytest.mark.core_invariant("TOOL-001")
def test_read_failure_retries_before_becoming_a_tool_message() -> None:
    attempts: list[int] = []

    @tool("read_failure_probe", response_format="content_and_artifact")
    def read_failure_probe(value: str):
        """Return a deterministic expected read failure."""

        def fail() -> ToolResult:
            attempts.append(len(attempts) + 1)
            return ToolResult(
                tool_name="read_failure_probe",
                success=False,
                error=f"expected-read-failure:{value}",
            )

        return invoke_native_tool("read_failure_probe", fail)

    configured = _configure_builtin_probe(read_failure_probe, "read")
    graph = build_fast_agent(
        _SingleToolCallModel(
            tool_name="read_failure_probe",
            tool_args={"value": "sentinel"},
        ),
        [configured],
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="read-failure-request")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "fast",
        },
        context=AssistantRunContext(),
    )
    message = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    )

    assert attempts == [1, 2, 3]
    assert message.status == "error"
    assert "expected-read-failure:sentinel" in str(message.content)


def _configure_builtin_probe(tool: BaseTool, effect: str) -> BaseTool:
    configure = getattr(native_boundary, "configure_builtin_tool", None)
    assert callable(configure)
    return configure(tool, effect)


def _invoke_default_toolnode(
    tool: BaseTool,
    args: dict[str, object],
) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = builder.compile().invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": tool.name,
                            "args": args,
                            "id": "call-default-toolnode",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        context=AssistantRunContext(),
    )
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message
