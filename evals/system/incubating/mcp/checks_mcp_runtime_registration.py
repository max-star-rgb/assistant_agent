"""Runtime wiring checks for explicitly enabled external MCP tools."""

import json

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.adapter import MCPToolDefinition
from assistant_agent.mcp.config import (
    MCPEmailToolMapping,
    MCPPersonalAssistantToolMapping,
    MCPServerConfig,
)
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


class _WeatherDiscoveryRunner:
    def list_tools(self, *, server):
        tool_name = (
            "get_weather_byDateTimeRange"
            if server.server_name == "weather_service"
            else "maps_weather"
        )
        return [
            MCPToolDefinition(
                name=tool_name,
                description="Get weather.",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ]

    def run_tool(self, **kwargs):  # pragma: no cover - registration only
        raise AssertionError(f"unexpected MCP execution: {kwargs}")


def test_real_registry_keeps_amap_as_the_only_explicit_weather_tool() -> None:
    generic_weather = MCPServerConfig(
        server_name="weather_service",
        command=["weather-server"],
        allowed_tools=["get_weather_byDateTimeRange"],
        read_only_tools=["get_weather_byDateTimeRange"],
        personal_assistant_tools=MCPPersonalAssistantToolMapping(
            weather_lookup="get_weather_byDateTimeRange",
        ),
    )
    amap = MCPServerConfig(
        server_name="amap_maps",
        command=["amap-server"],
        allowed_tools=["maps_weather"],
        read_only_tools=["maps_weather"],
    )
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
    )

    registry = create_default_registry(
        config,
        mcp_server_configs=[generic_weather, amap],
        mcp_runner=_WeatherDiscoveryRunner(),
        plugin_modules=[],
    )

    assert "weather" not in registry.list()
    assert "mcp.weather_service.get_weather_byDateTimeRange" not in registry.list()
    assert "mcp.amap_maps.maps_weather" in registry.list()


class _EmailDiscoveryRunner:
    def list_tools(self, *, server):
        assert server.server_name == "google_gmail_readonly"
        return [
            MCPToolDefinition(
                name=name,
                description="Read Gmail data.",
                input_schema={"type": "object", "properties": {}},
            )
            for name in (
                "search_gmail_messages",
                "get_gmail_messages_content_batch",
            )
        ]

    def run_tool(self, **kwargs):  # pragma: no cover - registration only
        raise AssertionError(f"unexpected MCP execution: {kwargs}")


def test_real_registry_exposes_stable_email_tools_without_raw_mapped_duplicates() -> None:
    server = MCPServerConfig(
        server_name="google_gmail_readonly",
        command=["workspace-mcp", "--tools", "gmail", "--read-only"],
        allowed_tools=[
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
        ],
        read_only_tools=[
            "search_gmail_messages",
            "get_gmail_messages_content_batch",
        ],
        email_tools=MCPEmailToolMapping(
            search="search_gmail_messages",
            read_batch="get_gmail_messages_content_batch",
            profile="workspace_mcp_v1",
            user_email="user@example.com",
        ),
    )
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
    )

    registry = create_default_registry(
        config,
        mcp_server_configs=[server],
        mcp_runner=_EmailDiscoveryRunner(),
        plugin_modules=[],
    )

    assert {"email_search", "email_read"}.issubset(registry.list())
    assert {
        "mcp.google_gmail_readonly.search_gmail_messages",
        "mcp.google_gmail_readonly.get_gmail_messages_content_batch",
    }.isdisjoint(registry.list())
