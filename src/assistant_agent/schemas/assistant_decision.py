"""Assistant decision schema for internal tool-call governance."""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from assistant_agent.schemas.planning import TaskPlan


AssistantDecisionType = Literal["final_answer", "tool_call", "ask_followup", "enter_plan_mode", "exit_plan_mode"]


class AssistantDecision(BaseModel):
    """Structured decision from the assistant node."""

    type: AssistantDecisionType = Field(description="Decision type: final_answer, tool_call, ask_followup, enter_plan_mode, or exit_plan_mode")
    message: str | None = Field(default=None, description="Response message for final_answer or ask_followup")
    tool_name: str | None = Field(default=None, description="Tool name for tool_call")
    tool_input: dict[str, Any] | None = Field(default=None, description="Tool input for tool_call")
    step_id: str | None = Field(default=None, description="Plan step id for plan-mode tool_call")
    plan: TaskPlan | None = Field(default=None, description="Task plan for enter_plan_mode")
    next_action: str | None = Field(default=None, description="exit_plan_mode next action: continue, final_answer, or ask_followup")
    reason: str | None = Field(default=None, description="Brief high-level decision reason, not chain-of-thought")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in {"final_answer", "tool_call", "ask_followup", "enter_plan_mode", "exit_plan_mode"}:
            return "final_answer"
        return v

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str | None, info: Any) -> str | None:
        decision_type = info.data.get("type")
        if decision_type in {"final_answer", "ask_followup"}:
            if not v or not v.strip():
                return "已处理请求。"
        return v

    @field_validator("tool_name", "tool_input")
    @classmethod
    def validate_tool_fields(cls, v: Any, info: Any) -> Any:
        decision_type = info.data.get("type")
        if decision_type == "tool_call":
            field_name = info.field_name
            if field_name == "tool_name" and (not v or not isinstance(v, str)):
                raise ValueError("tool_name must be a non-empty string for tool_call")
            if field_name == "tool_input" and v is not None and not isinstance(v, dict):
                raise ValueError("tool_input must be a dict for tool_call")
        return v

    @field_validator("next_action")
    @classmethod
    def validate_next_action(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in {"continue", "final_answer", "ask_followup"}:
            return "continue"
        return v


class NativeToolCall(BaseModel):
    """Provider-native tool call normalized at the adapter boundary."""

    id: str | None = None
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    provider_format: str = "openai_compatible"
    raw: dict[str, Any] = Field(default_factory=dict)

    def to_assistant_decision(self) -> AssistantDecision:
        """Convert to the internal decision protocol before validation/execution."""

        return AssistantDecision(
            type="tool_call",
            tool_name=self.name,
            tool_input=self.arguments,
            reason=f"Provider-native tool call ({self.provider_format}).",
            safety_notes=["native_tool_call"],
        )


def native_tool_call_to_assistant_decision(call: NativeToolCall) -> AssistantDecision:
    """Convert a normalized native tool call to AssistantDecision."""

    return call.to_assistant_decision()


def openai_tool_call_to_native_tool_call(payload: dict[str, Any]) -> NativeToolCall:
    """Parse an OpenAI-compatible tool call payload."""

    function = payload.get("function") if isinstance(payload.get("function"), dict) else {}
    name = function.get("name") or payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("native tool call missing function name")
    return NativeToolCall(
        id=str(payload.get("id")) if payload.get("id") is not None else None,
        name=name,
        arguments=_parse_native_arguments(function.get("arguments", payload.get("arguments"))),
        provider_format="openai_compatible",
        raw=payload,
    )


def openai_tool_call_to_assistant_decision(payload: dict[str, Any]) -> AssistantDecision:
    """Convert an OpenAI-compatible tool call payload to AssistantDecision."""

    return openai_tool_call_to_native_tool_call(payload).to_assistant_decision()


def _parse_native_arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if value.strip() else {}
        except json.JSONDecodeError:
            return {"__native_tool_arguments_error__": "arguments JSON parsing failed"}
        if isinstance(parsed, dict):
            return parsed
        return {"__native_tool_arguments_error__": "arguments JSON was not an object"}
    return {"__native_tool_arguments_error__": "arguments were not a JSON object"}
