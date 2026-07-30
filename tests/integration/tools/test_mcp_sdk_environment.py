"""Regression coverage for the external MCP subprocess environment."""

# Initialize through the runtime import path before crossing tools.__init__.
from assistant_agent.tools.registry import ToolRegistry as _ToolRegistry  # noqa: F401
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.mcp.sdk_client import (
    _mcp_subprocess_environment,
    _tool_result_from_sdk_response,
)
from assistant_agent.tools.observation import observation_from_tool_result


def test_mcp_subprocess_inherits_operator_proxy_environment(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:7890")
    monkeypatch.setenv("ALL_PROXY", "socks://proxy.example:7890")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    environment = _mcp_subprocess_environment({"PROVIDER_SETTING": "configured"})

    assert environment is not None
    assert environment["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert "ALL_PROXY" not in environment
    assert environment["NO_PROXY"] == "localhost,127.0.0.1"
    assert environment["PROVIDER_SETTING"] == "configured"


def test_mcp_server_environment_overrides_inherited_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://operator-proxy.example:7890")

    environment = _mcp_subprocess_environment(
        {"HTTPS_PROXY": "http://server-proxy.example:8080"}
    )

    assert environment is not None
    assert environment["HTTPS_PROXY"] == "http://server-proxy.example:8080"


def test_mcp_server_environment_resolves_explicit_parent_references(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id-sentinel")
    monkeypatch.delenv("MISSING_MCP_SECRET", raising=False)

    environment = _mcp_subprocess_environment(
        {
            "GOOGLE_OAUTH_CLIENT_ID": "${GOOGLE_OAUTH_CLIENT_ID}",
            "MISSING_MCP_SECRET": "${MISSING_MCP_SECRET}",
            "STATIC_SETTING": "static-sentinel",
        }
    )

    assert environment is not None
    assert environment["GOOGLE_OAUTH_CLIENT_ID"] == "client-id-sentinel"
    assert "MISSING_MCP_SECRET" not in environment
    assert environment["STATIC_SETTING"] == "static-sentinel"


def test_mcp_subprocess_keeps_all_proxy_when_no_protocol_proxy(monkeypatch) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://proxy.example:7890")

    environment = _mcp_subprocess_environment({})

    assert environment is not None
    assert environment["ALL_PROXY"] == "socks://proxy.example:7890"


def test_mcp_text_json_becomes_structured_model_observation() -> None:
    result = _tool_result_from_sdk_response(
        server=MCPServerConfig(
            server_name="amap_maps",
            command=["amap-server"],
            allowed_tools=["maps_weather"],
            read_only_tools=["maps_weather"],
        ),
        tool_name="maps_weather",
        namespaced_tool_name="mcp.amap_maps.maps_weather",
        response={
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"city":"上海市","forecasts":'
                        '[{"date":"2026-07-30","dayweather":"晴",'
                        '"daytemp":"36","nighttemp":"28"}]}'
                    ),
                }
            ],
            "isError": False,
        },
    )

    observation = observation_from_tool_result(result)

    assert observation.data == {
        "city": "上海市",
        "forecasts": [
            {
                "date": "2026-07-30",
                "dayweather": "晴",
                "daytemp": "36",
                "nighttemp": "28",
            }
        ],
    }
    assert observation.is_complete is True
