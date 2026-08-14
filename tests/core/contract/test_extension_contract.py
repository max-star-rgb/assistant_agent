from __future__ import annotations

import asyncio

from langchain_core.tools import BaseTool, StructuredTool
import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_mcp_tools,
    create_native_tools,
)


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
    tools = create_native_tools(
        ProviderConfig(provider_mode="mock"),
        resources=NativeToolResources(),
    )

    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert len({tool.name for tool in tools}) == len(tools)


@pytest.mark.core_invariant("EXT-001")
def test_mcp_extension_uses_allowlist_and_namespace() -> None:
    config = MCPServerConfig(
        server_name="server",
        command=["probe"],
        allowed_tools=["probe"],
        read_only_tools=["probe"],
        namespace_prefix="native",
    )

    tools = asyncio.run(create_mcp_tools([config], client_factory=_MCPClient))

    assert [tool.name for tool in tools] == ["native_server_probe"]
    assert tools[0].metadata["source"] == "mcp"
    assert tools[0].metadata["effect"] == "read"
