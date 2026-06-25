"""Tests for the Phase 8 Assistant Loop ReAct graph."""

import os
from unittest.mock import patch

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.assistant_decision import AssistantDecision
from multimodal_agent.schemas.requests import UserRequest


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


def test_assistant_decision_parsing_from_valid_json() -> None:
    """Verify AssistantDecision can parse valid JSON output."""
    json_output = """{
        "type": "final_answer",
        "message": "这是最终回答",
        "reason": "信息已足够"
    }"""
    decision = AssistantDecision.from_llm_output(json_output)
    assert decision.type == "final_answer"
    assert decision.message == "这是最终回答"
    assert decision.reason == "信息已足够"


def test_assistant_decision_parsing_tool_call() -> None:
    """Verify AssistantDecision can parse tool_call type decisions."""
    json_output = """{
        "type": "tool_call",
        "tool_name": "image_generation",
        "tool_input": {"prompt": "白色运动鞋"},
        "reason": "需要生成图片"
    }"""
    decision = AssistantDecision.from_llm_output(json_output)
    assert decision.type == "tool_call"
    assert decision.tool_name == "image_generation"
    assert decision.tool_input == {"prompt": "白色运动鞋"}


def test_assistant_decision_parsing_with_code_fence() -> None:
    """Verify AssistantDecision can extract JSON from markdown code fence."""
    output_with_fence = """模型前缀文本...
```json
{
    "type": "final_answer",
    "message": "从 code fence 中提取的回答"
}
```
其他文本..."""
    decision = AssistantDecision.from_llm_output(output_with_fence)
    assert decision.type == "final_answer"
    assert "从 code fence 中提取的回答" in decision.message


def test_assistant_decision_extracts_json_after_thought_prefix_without_exposing_it() -> None:
    """Verify non-JSON Thought prefixes are ignored and only the JSON decision is used."""
    output = """Thought: I should not expose this reasoning.
{"type": "tool_call", "tool_name": "product_search", "tool_input": {"query": "耳机"}, "reason": "需要搜索商品候选"}"""

    decision = AssistantDecision.from_llm_output(output)

    assert decision.type == "tool_call"
    assert decision.tool_name == "product_search"
    assert decision.tool_input == {"query": "耳机"}
    assert decision.reason == "需要搜索商品候选"
    assert "Thought" not in (decision.reason or "")


def test_assistant_decision_falls_back_to_final_answer_on_invalid_json() -> None:
    """Verify AssistantDecision falls back safely on invalid JSON."""
    invalid_json = "这不是 JSON，这只是文本回复"
    decision = AssistantDecision.from_llm_output(invalid_json)
    assert decision.type == "final_answer"
    assert decision.message == invalid_json


def test_assistant_decision_falls_back_on_missing_fields() -> None:
    """Verify AssistantDecision handles missing required fields gracefully."""
    incomplete_json = '{"type": "tool_call"}'  # missing tool_name
    decision = AssistantDecision.from_llm_output(incomplete_json)
    # Should fall back to final_answer
    assert decision.type == "final_answer"


def test_tool_registry_describe_tools_returns_safe_description() -> None:
    """Verify ToolRegistry.describe_tools() returns safe tool info (no secrets)."""
    from multimodal_agent.tools.registry import ToolRegistry, create_default_registry
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
    from multimodal_agent.agent.assistant_loop_graph import build_assistant_loop_graph
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
    from multimodal_agent.agent.assistant_loop_nodes import (
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

    # Test invalid LLM type falls back to default through the parser boundary
    decision = AssistantDecision.from_llm_output('{"type": "invalid_type", "message": "fallback"}')
    assert decision.type == "final_answer"


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
    assert config.video_provider == "mock"
