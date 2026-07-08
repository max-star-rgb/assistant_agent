from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_legacy_graph_helpers_delegate_runtime_dependency_assembly() -> None:
    for path in (
        "src/assistant_agent/agent/graph.py",
        "src/assistant_agent/agent/conditional_graph.py",
    ):
        source = _source(path)
        assert "create_memory_store(" not in source
        assert "MemoryManager(" not in source
        assert "ToolExecutor(" not in source
        assert "create_default_registry(" not in source
        assert "create_chat_adapter(" not in source
        assert "AgentGraphRuntime(" in source


def test_interfaces_do_not_bypass_tool_executor() -> None:
    for path in (
        "src/assistant_agent/api/routes_agent.py",
        "src/assistant_agent/api/websocket.py",
        "src/assistant_agent/mcp/server.py",
    ):
        source = _source(path)
        assert "registry.run(" not in source


def test_providers_do_not_depend_on_product_service_internals() -> None:
    provider_dir = Path("src/assistant_agent/providers")
    for path in provider_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from assistant_agent.services.product_adapter" not in source


def test_tools_providers_services_do_not_depend_on_engine_prompt_builder() -> None:
    for root in (
        Path("src/assistant_agent/tools"),
        Path("src/assistant_agent/providers"),
        Path("src/assistant_agent/services"),
    ):
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "from assistant_agent.agent.prompt_builder" not in source


def test_assistant_loop_does_not_reach_into_memory_retrieval_backend() -> None:
    source = _source("src/assistant_agent/agent/assistant_loop_nodes.py")

    assert "from assistant_agent.memory.retrieval" not in source
    assert "from assistant_agent.memory.store" not in source
    assert "from assistant_agent.memory.factory" not in source


def test_gateway_agent_adapter_does_not_route_agents_directly() -> None:
    source = _source("src/assistant_agent/realtime/agent_graph_backend.py")

    assert "AgentRouter" not in source
    assert "AgentRouteRequest" not in source
    assert "agent_router" not in source
    assert "route_request" not in source


def test_product_entry_layers_do_not_import_agent_graph_runtime_directly() -> None:
    for path in (
        "src/assistant_agent/api/routes_agent.py",
        "src/assistant_agent/api/websocket.py",
        "src/assistant_agent/api/gateway_runtime.py",
        "scripts/run_assistant_cli.py",
    ):
        source = _source(path)
        assert "from assistant_agent.agent.runtime import AgentGraphRuntime" not in source
        assert "AgentGraphRuntime(" not in source


def test_product_entry_layers_depend_on_runtime_app_boundary() -> None:
    for path in (
        "src/assistant_agent/api/routes_agent.py",
        "src/assistant_agent/api/websocket.py",
        "src/assistant_agent/api/gateway_runtime.py",
        "scripts/run_assistant_cli.py",
    ):
        source = _source(path)
        assert "AssistantRuntimeApp" in source or "get_assistant_runtime_app" in source
