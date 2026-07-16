"""Tests for the Phase 8 Assistant Loop ReAct graph."""

import os
from unittest.mock import patch

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision


def test_default_graph_mode_is_assistant_loop() -> None:
    """Verify the default graph mode uses the ReAct assistant loop."""
    config = ProviderConfig.from_env({})
    assert config.agent_graph_mode == "assistant_loop"


def test_assistant_loop_graph_mode_can_be_configured() -> None:
    """Verify assistant_loop mode can be enabled via environment variable."""
    config = ProviderConfig.from_env({"AGENT_GRAPH_MODE": "assistant_loop"})
    assert config.agent_graph_mode == "assistant_loop"


def test_max_tool_iterations_configured() -> None:
    """Verify max_tool_iterations can be configured."""
    config = ProviderConfig.from_env({"MAX_TOOL_ITERATIONS": "10"})
    assert config.max_tool_iterations == 10


def test_tool_registry_describe_tools_returns_safe_description() -> None:
    """Verify ToolRegistry.describe_tools() returns safe tool info (no secrets)."""
    from assistant_agent.tools.registry import create_default_registry
    registry = create_default_registry()
    tool_descriptions = registry.describe_tools()

    assert isinstance(tool_descriptions, list)
    assert len(tool_descriptions) > 0

    # Verify each tool has required fields
    for desc in tool_descriptions:
        assert "name" in desc
        assert "description" in desc
        assert "input_schema" in desc

        # Verify no sensitive info is included
        desc_str = str(desc).lower()
        assert "api_key" not in desc_str
        assert "secret" not in desc_str
        assert "authorization" not in desc_str
        assert "token" not in desc_str


def test_assistant_loop_graph_can_be_initialized_without_errors() -> None:
    """Verify the assistant loop graph can be built without errors."""
    # Test that the import works and graph can be built
    from assistant_agent.agent.assistant_loop_graph import build_assistant_loop_graph
    graph = build_assistant_loop_graph()
    assert graph is not None


def test_runtime_uses_assistant_loop_graph_by_default() -> None:
    """Verify runtime uses assistant_loop graph by default."""
    with patch.dict(os.environ, {}, clear=True):
        runtime = AgentGraphRuntime()
        # The actual graph instance is internal, but we verify the config is correct
        assert runtime.config.agent_graph_mode == "assistant_loop"


def test_runtime_can_use_assistant_loop_graph_via_config() -> None:
    """Verify runtime uses assistant_loop graph when configured."""
    config = ProviderConfig.from_env({"AGENT_GRAPH_MODE": "assistant_loop"})
    runtime = AgentGraphRuntime(config=config)
    assert runtime.config.agent_graph_mode == "assistant_loop"


def test_assistant_loop_nodes_import_without_errors() -> None:
    """Verify all assistant loop node functions can be imported."""
    from assistant_agent.agent.assistant_loop_nodes import (
        assistant_node,
        execute_requested_tool_node,
        route_after_assistant,
    )
    assert assistant_node is not None
    assert execute_requested_tool_node is not None
    assert route_after_assistant is not None


def test_assistant_decision_validation() -> None:
    """Verify AssistantDecision validation logic."""
    # Test valid final_answer
    decision = AssistantDecision(type="final_answer", message="回答")
    assert decision.type == "final_answer"

    # Test valid ask_followup
    decision = AssistantDecision(type="ask_followup", message="需要更多信息")
    assert decision.type == "ask_followup"


def test_assistant_decision_tool_validation() -> None:
    """Verify tool_call validation."""
    # Test valid tool_call
    decision = AssistantDecision(
        type="tool_call",
        tool_name="image_generation",
        tool_input={"prompt": "test"}
    )
    assert decision.type == "tool_call"
    assert decision.tool_name == "image_generation"
    assert decision.tool_input == {"prompt": "test"}


def test_no_real_external_api_calls_in_tests() -> None:
    """Verify all tests use mock/local providers (no real external calls)."""
    config = ProviderConfig.from_env({})
    # All providers should default to mock/offline
    assert config.vision_provider == "mock"
    assert config.chat_provider == "mock"
    assert config.image_generation_provider == "mock"
    assert config.product_search_provider == "mock"
    assert config.price_compare_provider == "mock"
    assert config.render_provider == "mock"
    assert not hasattr(config, "video_provider")
