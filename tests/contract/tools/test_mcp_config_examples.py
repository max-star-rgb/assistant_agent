"""Contract checks for deployable MCP configuration examples."""

import json
from pathlib import Path

from assistant_agent.mcp.config import MCPServerConfig


PROJECT_ROOT = Path(__file__).resolve().parents[3]

AMAP_READ_TOOLS = {
    "maps_geo",
    "maps_ip_location",
    "maps_weather",
    "maps_bicycling",
    "maps_direction_walking",
    "maps_direction_driving",
    "maps_direction_transit_integrated",
    "maps_text_search",
    "maps_around_search",
}


def test_mcp_example_allowlists_only_read_only_amap_tools() -> None:
    payload = json.loads(
        (PROJECT_ROOT / "deploy" / "mcp_servers.example.json").read_text(
            encoding="utf-8"
        )
    )
    servers = [MCPServerConfig.model_validate(item) for item in payload["servers"]]

    amap = next(server for server in servers if server.server_name == "amap_maps")
    calendar = next(
        server for server in servers if server.server_name == "google_calendar"
    )

    assert all(
        server.personal_assistant_tools.weather_lookup is None
        for server in servers
    )
    assert set(amap.allowed_tools) == AMAP_READ_TOOLS
    assert set(amap.read_only_tools) == AMAP_READ_TOOLS
    assert not hasattr(amap, "enabled_tools")
    assert amap.command == [
        "/usr/bin/npx",
        "-y",
        "@amap/amap-maps-mcp-server@0.0.8",
    ]
    assert amap.env == {
        "AMAP_MAPS_API_KEY": "<set-in-ignored-local-config>",
    }
    assert calendar.command[-2:] == ["--tools", "calendar"]
