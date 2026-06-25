from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_legacy_graph_helpers_delegate_runtime_dependency_assembly() -> None:
    for path in (
        "src/multimodal_agent/agent/graph.py",
        "src/multimodal_agent/agent/conditional_graph.py",
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
        "src/multimodal_agent/api/routes_agent.py",
        "src/multimodal_agent/api/websocket.py",
        "src/multimodal_agent/mcp/server.py",
    ):
        source = _source(path)
        assert "registry.run(" not in source


def test_providers_do_not_depend_on_product_service_internals() -> None:
    provider_dir = Path("src/multimodal_agent/providers")
    for path in provider_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from multimodal_agent.services.product_adapter" not in source


def test_tools_providers_services_do_not_depend_on_engine_prompt_builder() -> None:
    for root in (
        Path("src/multimodal_agent/tools"),
        Path("src/multimodal_agent/providers"),
        Path("src/multimodal_agent/services"),
    ):
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "from multimodal_agent.agent.prompt_builder" not in source


def test_assistant_loop_does_not_reach_into_memory_retrieval_backend() -> None:
    source = _source("src/multimodal_agent/agent/assistant_loop_nodes.py")

    assert "from multimodal_agent.memory.retrieval" not in source
    assert "from multimodal_agent.memory.store" not in source
    assert "from multimodal_agent.memory.factory" not in source
