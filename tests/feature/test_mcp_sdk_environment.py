"""Regression coverage for the external MCP subprocess environment."""

# Initialize through the runtime import path before crossing tools.__init__.
from assistant_agent.tools.registry import ToolRegistry as _ToolRegistry  # noqa: F401
from assistant_agent.mcp.sdk_client import _mcp_subprocess_environment


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


def test_mcp_subprocess_keeps_all_proxy_when_no_protocol_proxy(monkeypatch) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://proxy.example:7890")

    environment = _mcp_subprocess_environment({})

    assert environment is not None
    assert environment["ALL_PROXY"] == "socks://proxy.example:7890"
