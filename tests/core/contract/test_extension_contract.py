from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from blockbuster import blockbuster_ctx
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langchain_core.tools import BaseTool, StructuredTool
import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.mcp.stateful_sessions import ThreadMcpSessionPool
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)
from assistant_agent.runtime import thread_resources as thread_resources_module
from assistant_agent.runtime.thread_resources import (
    ThreadResourceConfig,
    ThreadResourceManager,
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
    tools = asyncio.run(
        create_native_tool_inventory(
            ProviderConfig(provider_mode="mock"),
            resources=NativeToolResources(),
            mcp_server_configs=[],
        )
    )

    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert len({tool.name for tool in tools}) == len(tools)
    assert all(tool.metadata["source"] == "builtin" for tool in tools)
    assert all("effect" not in tool.metadata for tool in tools)
    tool_names = {tool.name for tool in tools}
    assert "file_read" not in tool_names
    assert "read_file" not in tool_names
    assert "load_skill" not in tool_names
    assert "load_skill_reference" not in tool_names


@pytest.mark.core_invariant("EXT-001")
def test_mcp_extension_uses_allowlist_and_namespace() -> None:
    config = MCPServerConfig(
        server_name="server",
        command=["probe"],
        allowed_tools=["probe"],
        auto_approved_tools=["probe"],
        namespace_prefix="native",
    )

    tools = asyncio.run(
        create_native_tool_inventory(
            ProviderConfig(provider_mode="mock"),
            resources=NativeToolResources(),
            mcp_server_configs=[config],
            mcp_client_factory=_MCPClient,
        )
    )

    tool = next(tool for tool in tools if tool.name == "native_server_probe")
    assert tool.metadata["source"] == "mcp"
    assert "effect" not in tool.metadata


@pytest.mark.core_invariant("EXT-001")
def test_mcp_legacy_read_only_list_migrates_to_auto_approve() -> None:
    config = MCPServerConfig.model_validate(
        {
            "server_name": "server",
            "command": ["probe"],
            "allowed_tools": ["probe"],
            "read_only_tools": ["probe"],
        }
    )

    assert config.auto_approved_tools == ["probe"]
    assert not hasattr(config, "read_only_tools")


@pytest.mark.core_invariant("EXT-001")
def test_thread_mcp_creates_resources_off_the_event_loop(tmp_path: Path) -> None:
    config = MCPServerConfig(
        server_name="browser",
        session_scope="thread",
        command=["probe"],
        allowed_tools=["navigate"],
    )
    manager = ThreadResourceManager(ThreadResourceConfig(root=tmp_path / "threads"))
    pool = ThreadMcpSessionPool([config], manager=manager)
    session = SimpleNamespace(call_tool=AsyncMock(return_value="ok"))

    async def scenario() -> object:
        pool._entry = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                call_lock=asyncio.Lock(),
                session=session,
            )
        )
        request = MCPToolCallRequest(
            name="navigate",
            args={},
            server_name="browser",
            runtime=SimpleNamespace(
                server_info=SimpleNamespace(
                    user=SimpleNamespace(identity="user-sentinel")
                ),
                execution_info=SimpleNamespace(thread_id="thread-sentinel"),
            ),
        )
        with blockbuster_ctx(scanned_modules=thread_resources_module):
            return await pool.call(request)

    assert asyncio.run(scenario()) == "ok"
