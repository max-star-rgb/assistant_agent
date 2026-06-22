"""Assistant decision schema for ReAct-style reasoning."""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


AssistantDecisionType = Literal["final_answer", "tool_call", "ask_followup"]


class AssistantDecision(BaseModel):
    """Structured decision from the assistant node."""

    type: AssistantDecisionType = Field(description="Decision type: final_answer, tool_call, or ask_followup")
    message: str | None = Field(default=None, description="Response message for final_answer or ask_followup")
    tool_name: str | None = Field(default=None, description="Tool name for tool_call")
    tool_input: dict[str, Any] | None = Field(default=None, description="Tool input for tool_call")
    reason: str | None = Field(default=None, description="Explanation of the decision")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in {"final_answer", "tool_call", "ask_followup"}:
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
            if decision_type not in {"final_answer", "tool_call", "ask_followup"}:
                decision_type = "final_answer"

            message = parsed.get("message")
            tool_name = parsed.get("tool_name")
            tool_input = parsed.get("tool_input")
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

            return cls(
                type=decision_type,
                message=message,
                tool_name=tool_name,
                tool_input=tool_input,
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
