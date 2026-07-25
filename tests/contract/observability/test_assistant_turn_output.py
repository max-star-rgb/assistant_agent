import pytest
from pydantic import TypeAdapter, ValidationError

from assistant_agent.schemas.assistant_output import (
    AssistantTextOutput,
    AssistantToolCall,
    AssistantTurnOutput,
)


def test_assistant_turn_output_accepts_only_text_or_tool_call() -> None:
    adapter = TypeAdapter(AssistantTurnOutput)

    assert adapter.validate_python(
        {"type": "text", "text": "text-sentinel"}
    ) == AssistantTextOutput(text="text-sentinel")
    assert adapter.validate_python(
        {
            "type": "tool_call",
            "tool_name": "tool-sentinel",
            "tool_input": {"value": "input-sentinel"},
        }
    ) == AssistantToolCall(
        tool_name="tool-sentinel",
        tool_input={"value": "input-sentinel"},
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "final_answer", "message": "legacy-sentinel"},
        {"type": "ask_followup", "message": "legacy-sentinel"},
        {"type": "enter_plan_mode", "plan": {}},
        {"type": "exit_plan_mode", "next_action": "continue"},
        {"type": "text", "text": ""},
        {"type": "text", "text": "   "},
        {"type": "text", "text": "text-sentinel", "tool_name": "tool-sentinel"},
        {"type": "tool_call", "tool_name": "", "tool_input": {}},
        {"type": "tool_call", "tool_name": "   ", "tool_input": {}},
        {
            "type": "tool_call",
            "tool_name": "tool-sentinel",
            "tool_input": {},
            "text": "text-sentinel",
        },
    ],
)
def test_assistant_turn_output_rejects_legacy_empty_and_cross_variant_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(AssistantTurnOutput).validate_python(payload)
