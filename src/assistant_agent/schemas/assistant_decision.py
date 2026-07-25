"""Compatibility imports for the removed multi-shape assistant decision model."""

from typing import Any

from assistant_agent.schemas.assistant_output import (
    AssistantTextOutput,
    AssistantToolCall,
    AssistantTurnOutput,
    NativeToolCall,
    native_tool_call_to_assistant_output,
    openai_tool_call_to_native_tool_call,
)


AssistantDecision = AssistantToolCall


def native_tool_call_to_assistant_decision(call: NativeToolCall) -> AssistantToolCall:
    """Compatibility wrapper for older direct tool-governance callers."""

    return native_tool_call_to_assistant_output(call)


def openai_tool_call_to_assistant_decision(payload: dict[str, Any]) -> AssistantToolCall:
    """Convert an OpenAI-compatible tool call payload to a governed tool call."""

    return openai_tool_call_to_native_tool_call(payload).to_assistant_tool_call()
