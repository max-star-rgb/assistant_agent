from assistant_agent.schemas.assistant_decision import (
    native_tool_call_to_assistant_decision,
    openai_tool_call_to_assistant_decision,
    openai_tool_call_to_native_tool_call,
)


def test_openai_tool_call_to_native_tool_call_parses_arguments_json() -> None:
    call = openai_tool_call_to_native_tool_call(
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "product_search", "arguments": '{"query": "耳机"}'},
        }
    )

    assert call.id == "call_1"
    assert call.name == "product_search"
    assert call.arguments == {"query": "耳机"}
    assert call.provider_format == "openai_compatible"


def test_openai_tool_call_to_assistant_decision_uses_internal_protocol() -> None:
    decision = openai_tool_call_to_assistant_decision(
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "product_search", "arguments": {"query": "耳机"}},
        }
    )

    assert decision.type == "tool_call"
    assert decision.tool_name == "product_search"
    assert decision.tool_input == {"query": "耳机"}
    assert decision.safety_notes == ["native_tool_call"]


def test_native_tool_call_malformed_arguments_stays_validator_visible() -> None:
    decision = openai_tool_call_to_assistant_decision(
        {
            "type": "function",
            "function": {"name": "product_search", "arguments": '{"query": "耳机",}'},
        }
    )

    assert decision.type == "tool_call"
    assert decision.tool_name == "product_search"
    assert decision.tool_input == {"__native_tool_arguments_error__": "arguments JSON parsing failed"}


def test_native_tool_call_to_assistant_decision_helper() -> None:
    call = openai_tool_call_to_native_tool_call(
        {"function": {"name": "image_generation", "arguments": '{"prompt": "海报"}'}}
    )

    decision = native_tool_call_to_assistant_decision(call)

    assert decision.type == "tool_call"
    assert decision.tool_name == "image_generation"
    assert decision.tool_input == {"prompt": "海报"}
