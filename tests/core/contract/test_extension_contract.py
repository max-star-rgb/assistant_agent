from __future__ import annotations

import asyncio

from langchain_core.tools import BaseTool, StructuredTool
import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.skills.loading import SkillCatalog


class _MCPClient:
    def __init__(self, _connections, **_kwargs) -> None:
        pass

    async def get_tools(self, *, server_name=None):
        def probe(value: str) -> str:
            """probe"""

            return value

        return [StructuredTool.from_function(probe, name="probe")]


@pytest.mark.core_invariant("EXT-001")
def test_native_extensions_are_static_standard_tools() -> None:
    tools = asyncio.run(
        create_native_tool_inventory(
            ProviderConfig(provider_mode="mock"),
            resources=NativeToolResources(),
            mcp_server_configs=[],
            skill_catalog=SkillCatalog(descriptors=[]),
        )
    )

    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert len({tool.name for tool in tools}) == len(tools)
    assert all(tool.metadata["source"] == "builtin" for tool in tools)
    assert all(
        tool.metadata["effect"] in {"read", "generate", "write", "dangerous"}
        for tool in tools
    )


@pytest.mark.core_invariant("EXT-001")
def test_mcp_extension_uses_allowlist_and_namespace() -> None:
    config = MCPServerConfig(
        server_name="server",
        command=["probe"],
        allowed_tools=["probe"],
        read_only_tools=["probe"],
        namespace_prefix="native",
    )

    tools = asyncio.run(
        create_native_tool_inventory(
            ProviderConfig(provider_mode="mock"),
            resources=NativeToolResources(),
            mcp_server_configs=[config],
            mcp_client_factory=_MCPClient,
            skill_catalog=SkillCatalog(descriptors=[]),
        )
    )

    tool = next(tool for tool in tools if tool.name == "native_server_probe")
    assert tool.metadata["source"] == "mcp"
    assert tool.metadata["effect"] == "read"
