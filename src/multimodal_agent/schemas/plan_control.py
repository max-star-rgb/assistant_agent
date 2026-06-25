"""Plan-and-solve controller decision schema."""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


PlanControllerDecisionType = Literal["execute_step", "replan", "ask_followup", "final_answer"]


class PlanControllerDecision(BaseModel):
    """Structured decision from the plan controller LLM."""

    type: PlanControllerDecisionType
    step_id: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    reason: str | None = None
    missing_slots: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in {"execute_step", "replan", "ask_followup", "final_answer"}:
            return "final_answer"
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str | None, info: Any) -> str | None:
        decision_type = info.data.get("type")
        if decision_type in {"ask_followup", "final_answer"} and (not value or not value.strip()):
            return "已处理请求。"
        return value

    @field_validator("tool_input")
    @classmethod
    def validate_tool_input(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("tool_input must be a JSON object.")
        return value

    @classmethod
    def from_llm_output(cls, text: str) -> "PlanControllerDecision":
        """Parse LLM output into a plan controller decision."""

        json_str = _extract_json(text)
        if not json_str:
            return cls(
                type="final_answer",
                message=text.strip() or "计划控制器没有返回可执行决策。",
                reason="No valid JSON found, treated as final_answer.",
                safety_notes=["invalid_controller_output"],
            )

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            return cls(
                type="final_answer",
                message=text.strip() or "计划控制器输出不是合法 JSON。",
                reason="JSON parsing failed, treated as final_answer.",
                safety_notes=["invalid_controller_output"],
            )
        if not isinstance(parsed, dict):
            return cls(
                type="final_answer",
                message=text.strip() or "计划控制器输出不是 JSON 对象。",
                reason="JSON was not an object, treated as final_answer.",
                safety_notes=["invalid_controller_output"],
            )

        decision_type = parsed.get("type", "final_answer")
        if decision_type not in {"execute_step", "replan", "ask_followup", "final_answer"}:
            decision_type = "final_answer"
        if decision_type == "execute_step" and not parsed.get("step_id"):
            return cls(
                type="final_answer",
                message="计划控制器没有指定要执行的 step_id，已停止执行。",
                reason="execute_step missing step_id.",
                safety_notes=["missing_step_id"],
            )
        try:
            return cls(
                type=decision_type,
                step_id=parsed.get("step_id"),
                tool_input=parsed.get("tool_input") or {},
                message=parsed.get("message"),
                reason=parsed.get("reason"),
                missing_slots=parsed.get("missing_slots") if isinstance(parsed.get("missing_slots"), list) else [],
                safety_notes=parsed.get("safety_notes") if isinstance(parsed.get("safety_notes"), list) else [],
            )
        except ValueError:
            return cls(
                type="final_answer",
                message="计划控制器工具输入格式无效，未执行工具。",
                reason="tool_input was not a JSON object.",
                safety_notes=["invalid_tool_input"],
            )


def _extract_json(text: str) -> str | None:
    fenced_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(fenced_pattern, text or "", re.DOTALL)
    if match:
        return match.group(1)
    open_brace = (text or "").find("{")
    if open_brace == -1:
        return None
    brace_count = 0
    for index, char in enumerate((text or "")[open_brace:], start=open_brace):
        if char == "{":
            brace_count += 1
        elif char == "}":
            brace_count -= 1
            if brace_count == 0:
                return text[open_brace : index + 1]
    return None
