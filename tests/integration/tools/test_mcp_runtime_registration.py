"""Runtime wiring checks for explicitly enabled external MCP tools."""

import json

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.adapter import MCPToolDefinition
from assistant_agent.tools.plugins.registry_factory import create_default_registry


class _DiscoveryRunner:
    def list_tools(self, *, server):
        assert server.server_name == "amap_maps"
        return [
            MCPToolDefinition(
                name="maps_text_search",
                description="Search places.",
                input_schema={
                    "type": "object",
                    "properties": {"keywords": {"type": "string"}},
                    "required": ["keywords"],
                },
            )
        ]

    def run_tool(self, **kwargs):  # pragma: no cover - registration only
        raise AssertionError(f"unexpected MCP execution: {kwargs}")


def test_real_registry_registers_environment_enabled_generic_mcp_tools(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_name": "amap_maps",
                        "transport": "stdio",
                        "command": ["/usr/bin/npx", "amap-server"],
                        "allowed_tools": ["maps_text_search"],
                        "read_only_tools": ["maps_text_search"],
                        "enabled_tools": ["maps_text_search"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MULTIMODAL_AGENT_MCP_ENABLED", "1")
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
    )

    registry = create_default_registry(
        config,
        mcp_config_path=str(config_path),
        mcp_runner=_DiscoveryRunner(),
        plugin_modules=[],
    )

    tool_name = "mcp.amap_maps.maps_text_search"
    assert tool_name in registry.list()
    assert registry.registration_record(tool_name).source_type == "mcp"
