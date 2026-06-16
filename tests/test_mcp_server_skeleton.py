from multimodal_agent.mcp.server import OfflineMCPServer


def test_offline_mcp_server_lists_tools() -> None:
    server = OfflineMCPServer()

    names = {tool["name"] for tool in server.list_tools()}

    assert {"agent_run", "tool_list", "tool_run", "demo_flow_run"}.issubset(names)


def test_offline_mcp_agent_run_uses_runtime() -> None:
    result = OfflineMCPServer().call_tool(
        "agent_run",
        {"user_id": "u1", "session_id": "s1", "text": "帮我写一段商品介绍"},
    )

    assert result.status == "succeeded"
    assert result.data["response_text"]
    assert result.metadata["offline"] is True


def test_offline_mcp_tool_run_uses_registry() -> None:
    result = OfflineMCPServer().call_tool(
        "tool_run",
        {"tool_name": "product_search", "input": {"query": "白色运动鞋"}},
    )

    assert result.status == "succeeded"
    assert result.data["tool_name"] == "product_search"
    assert result.metadata["registry_tool"] == "product_search"


def test_offline_mcp_errors_are_redacted() -> None:
    result = OfflineMCPServer().call_tool("missing_tool", {"Authorization": "Bearer secret-token"})

    payload = result.model_dump_json()
    assert result.status == "failed"
    assert "secret-token" not in payload
    assert "Authorization" not in payload
