"""RED/GREEN coverage for LangChain-native local and MCP tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.video_adapter import MockVideoUnderstandingAdapter
from assistant_agent.media.vision.models import VisionUnderstandingRequest
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_mcp_tools,
    create_native_tools,
    mcp_connections,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.media_inspection.tool import MediaInspectTool


class IdentityProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1)
    user_id: str = Field(min_length=1)


class IdentityProbeTool(ToolBase):
    name = "identity_probe"
    description = "Return the trusted runtime user with the probe query."
    input_schema = IdentityProbeInput
    output_schema = IdentityProbeInput
    category = "read"
    runtime_input_bindings = (
        RuntimeInputBinding(
            field="user_id",
            source="runtime_identity",
            key="user_id",
        ),
    )

    def _execute(self, input: IdentityProbeInput, context: ToolContext) -> ToolResult:
        data = {
            "query": input.query,
            "user_id": input.user_id,
            "context_user_id": context.user_id,
        }
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation={"summary": f"{input.user_id}:{input.query}"},
        )


def test_native_tool_hides_and_injects_runtime_identity() -> None:
    """Catches exposing trusted identity as an LLM-owned argument."""

    native_tool = IdentityProbeTool()
    assert set(native_tool.tool_call_schema.model_fields) == {"query"}
    assert set(native_tool.args_schema.model_fields) == {"query", "runtime"}

    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([native_tool]))
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
                                "name": "identity_probe",
                                "args": {"query": "sentinel"},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            context=AssistantRunContext(
                user_id="user-1",
                tenant_id="tenant-1",
            ),
        )
    )

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert json.loads(message.content) == {"summary": "user-1:sentinel"}
    assert message.artifact == {
        "query": "sentinel",
        "user_id": "user-1",
        "context_user_id": "user-1",
    }


def test_native_tool_cannot_be_called_through_legacy_direct_run() -> None:
    """Catches bypassing ToolRuntime identity injection through ToolBase.run."""

    with pytest.raises(ValidationError):
        IdentityProbeTool().run(
            {
                "query": "sentinel",
                "user_id": "forged-user",
            }
        )


def test_media_inspect_explicit_video_uses_internal_legacy_boundary() -> None:
    """Catches routing an internal ToolBase branch through native BaseTool.run."""

    result = MediaInspectTool(video_adapter=MockVideoUnderstandingAdapter()).run_legacy(
        VisionUnderstandingRequest(video_ids=["video-sentinel"]),
        ToolContext(),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["summary"]


def test_mock_native_tool_assembly_is_static_and_unique() -> None:
    tools = create_native_tools(
        ProviderConfig(provider_mode="mock"),
        resources=NativeToolResources(),
    )

    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names))


def test_builtin_inventory_returns_concrete_native_tools_without_adapter() -> None:
    """Catches retaining the legacy Tool -> StructuredTool production bridge."""

    tools = create_native_tools(
        ProviderConfig(provider_mode="mock"),
        resources=NativeToolResources(),
    )

    assert tools
    assert all(
        type(tool).__module__.startswith("assistant_agent.tools.plugins.builtin.")
        for tool in tools
    )


def test_mcp_connections_preserve_explicit_stdio_configuration() -> None:
    config = MCPServerConfig(
        server_name="maps",
        command=["python", "server.py"],
        cwd="/srv/maps",
        env={"TOKEN": "literal"},
        allowed_tools=["search"],
        read_only_tools=["search"],
        namespace_prefix="geo",
    )

    assert mcp_connections([config]) == {
        "maps": {
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
            "cwd": "/srv/maps",
            "env": {"TOKEN": "literal"},
        }
    }


class FakeMCPClient:
    def __init__(self, _connections, **_kwargs) -> None:
        pass

    async def get_tools(self, *, server_name: str | None = None) -> list[BaseTool]:
        from langchain_core.tools import StructuredTool

        def search(query: str) -> str:
            """Search a fixture server."""

            return query

        def hidden(query: str) -> str:
            """A tool outside the allowlist."""

            return query

        assert server_name == "maps"
        return [
            StructuredTool.from_function(search, name="search"),
            StructuredTool.from_function(hidden, name="hidden"),
        ]


def test_mcp_tools_use_official_client_boundary_and_allowlist() -> None:
    config = MCPServerConfig(
        server_name="maps",
        command=["python", "server.py"],
        allowed_tools=["search"],
        read_only_tools=["search"],
        namespace_prefix="geo",
    )

    tools = asyncio.run(create_mcp_tools([config], client_factory=FakeMCPClient))

    assert [tool.name for tool in tools] == ["geo_maps_search"]
