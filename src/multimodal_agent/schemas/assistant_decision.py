"""Assistant decision schema for the public ReAct decision protocol."""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from multimodal_agent.schemas.planning import TaskPlan


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

    @classmethod
    def from_llm_output(cls, text: str) -> "AssistantDecision":
        """Parse LLM output into an AssistantDecision safely."""
        if not text or not text.strip():
            return cls(
                type="final_answer",
                message="已处理请求。",
                reason="Empty or whitespace-only output.",
            )

        json_str = _extract_json(text)
        if not json_str:
            return cls(
                type="final_answer",
                message=text.strip(),
                reason="No valid JSON found, treated as final_answer.",
            )

        try:
            parsed = json.loads(json_str)
            if not isinstance(parsed, dict):
                return cls(
                    type="final_answer",
                    message=text.strip(),
                    reason="JSON was not an object, treated as final_answer.",
                )

            decision_type = parsed.get("type", "final_answer")
            if decision_type not in {"final_answer", "tool_call", "ask_followup", "enter_plan_mode", "exit_plan_mode"}:
                decision_type = "final_answer"

            message = parsed.get("message")
            tool_name = parsed.get("tool_name")
            tool_input = parsed.get("tool_input")
            step_id = parsed.get("step_id")
            plan = _parse_plan_payload(parsed.get("plan"))
            next_action = parsed.get("next_action")
            reason = parsed.get("reason")
            confidence = parsed.get("confidence")
            missing_slots = parsed.get("missing_slots")
            safety_notes = parsed.get("safety_notes")

            if decision_type == "tool_call":
                if not tool_name:
                    decision_type = "final_answer"
                    if not message:
                        message = text.strip()
                if tool_input is None:
                    tool_input = {}
                elif not isinstance(tool_input, dict):
                    return cls(
                        type="final_answer",
                        message="工具输入格式无效，未执行工具。",
                        reason="tool_input was not a JSON object.",
                        safety_notes=["invalid_tool_input"],
                    )

            if decision_type in {"final_answer", "ask_followup"}:
                if not message:
                    message = text.strip()
            if decision_type == "exit_plan_mode" and not next_action:
                next_action = "continue"

            return cls(
                type=decision_type,
                message=message,
                tool_name=tool_name,
                tool_input=tool_input,
                step_id=step_id if isinstance(step_id, str) and step_id.strip() else None,
                plan=plan,
                next_action=next_action if isinstance(next_action, str) else None,
                reason=reason,
                confidence=confidence if isinstance(confidence, int | float) else None,
                missing_slots=missing_slots if isinstance(missing_slots, list) else [],
                safety_notes=safety_notes if isinstance(safety_notes, list) else [],
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls(
                type="final_answer",
                message=text.strip(),
                reason="JSON parsing failed, treated as final_answer.",
            )


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


def _parse_plan_payload(value: Any) -> TaskPlan | None:
    if value is None:
        return None
    if isinstance(value, TaskPlan):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return TaskPlan.model_validate(value)
    except Exception:
        return None


def _extract_json(text: str) -> str | None:
    """Extract JSON object from text, handling code fences."""
    fenced_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(fenced_pattern, text, re.DOTALL)
    if match:
        return match.group(1)

    open_brace = text.find("{")
    if open_brace == -1:
        return None

    brace_count = 0
    for i, char in enumerate(text[open_brace:], start=open_brace):
        if char == "{":
            brace_count += 1
        elif char == "}":
            brace_count -= 1
            if brace_count == 0:
                return text[open_brace : i + 1]

    return None


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
