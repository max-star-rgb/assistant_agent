from assistant_agent.mcp.server import OfflineMCPServer


def test_offline_mcp_server_lists_tools() -> None:
    server = OfflineMCPServer()

    names = {tool["name"] for tool in server.list_tools()}

    assert {"agent_run", "tool_list", "tool_run", "demo_flow_run"}.issubset(names)
    tool_run = next(tool for tool in server.list_tools() if tool["name"] == "tool_run")
    assert tool_run["input_schema"]["fields"]["tool_name"]["required"] is True


def test_offline_mcp_tool_list_includes_registry_tool_specs() -> None:
    result = OfflineMCPServer().call_tool("tool_list", {})

    specs = result.data["registry_tool_specs"]
    video = next(spec for spec in specs if spec["name"] == "video_understanding")

    assert result.status == "succeeded"
    assert video["input_schema"]["fields"]
    assert "video_ids" in " ".join(video["runtime_constraints"])
    assert video["when_to_use"]


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


def test_offline_mcp_tool_run_uses_action_validator() -> None:
    result = OfflineMCPServer().call_tool(
        "tool_run",
        {"tool_name": "product_search", "input": {}},
    )

    assert result.status == "failed"
    assert result.errors[0]["code"] == "invalid_tool_input"
    assert result.data["validator_result"]["accepted"] is False


def test_offline_mcp_errors_are_redacted() -> None:
    result = OfflineMCPServer().call_tool("missing_tool", {"Authorization": "Bearer secret-token"})

    payload = result.model_dump_json()
    assert result.status == "failed"
    assert "secret-token" not in payload
    assert "Authorization" not in payload
