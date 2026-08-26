import asyncio

from assistant_agent.config import ProviderConfig
from assistant_agent.native_agent.tools import (
    NativeToolResources,
    create_native_tool_inventory,
)


def test_native_inventory_does_not_expose_python_interpreter() -> None:
    tools = asyncio.run(
        create_native_tool_inventory(
            ProviderConfig(provider_mode="mock"),
            resources=NativeToolResources(),
            mcp_server_configs=[],
        )
    )

    assert "python_interpreter" not in {tool.name for tool in tools}
