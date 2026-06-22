"""Action validation for the assistant ReAct loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from multimodal_agent.agent.state import AgentState
from multimodal_agent.schemas.assistant_decision import AssistantDecision
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.tools.registry import ToolRegistry


class ActionValidationResult(BaseModel):
    """Result of validating an assistant-proposed tool action."""

    accepted: bool
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ActionValidator:
    """Validate tool actions before ToolExecutor runs them."""

    def validate(
        self,
        *,
        decision: AssistantDecision,
        registry: ToolRegistry,
        request: UserRequest,
        state: AgentState,
    ) -> ActionValidationResult:
        if decision.type != "tool_call":
            return ActionValidationResult(accepted=True, code="not_tool_call", message="No tool execution required.")
        if not decision.tool_name:
            return _reject("missing_tool_name", "tool_call must include tool_name.")
        if not isinstance(decision.tool_input, dict):
            return _reject("invalid_tool_input", "tool_input must be a JSON object.")

        tool_name = decision.tool_name
        if tool_name not in registry.list():
            return _reject("unknown_tool", f"Unknown tool: {tool_name}.")

        media_error = _validate_required_media(tool_name, decision.tool_input, request)
        if media_error is not None:
            return media_error

        semantic_error = _validate_required_semantic_inputs(tool_name, decision.tool_input)
        if semantic_error is not None:
            return semantic_error

        if tool_name == "render_3d" and not _has_explicit_render_intent(request.text or "", decision.tool_input):
            return _reject(
                "render_intent_required",
                "render_3d requires explicit 3D, render, modeling, or scene-preview intent.",
            )

        try:
            registry.get(tool_name).input_schema.model_validate(decision.tool_input)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"msg": "invalid input"}
            return _reject("invalid_tool_input", f"{tool_name} input invalid: {first.get('msg', 'invalid input')}")

        return ActionValidationResult(accepted=True, code="accepted", message="Action accepted.")


def _validate_required_media(
    tool_name: str,
    tool_input: dict[str, Any],
    request: UserRequest,
) -> ActionValidationResult | None:
    if tool_name == "vision_understanding":
        image_ids = tool_input.get("image_ids") or request.image_ids
        if not image_ids:
            return _reject("missing_required_input", "vision_understanding requires image_ids.")
    if tool_name == "video_understanding":
        video_ref = tool_input.get("video_ref")
        video_ids = tool_input.get("video_ids") or request.video_ids
        if not video_ref and not video_ids:
            return _reject("missing_required_input", "video_understanding requires video_ref or video_ids.")
    return None


def _validate_required_semantic_inputs(tool_name: str, tool_input: dict[str, Any]) -> ActionValidationResult | None:
    if tool_name == "image_generation":
        if not any(
            tool_input.get(key)
            for key in ("prompt", "product_id", "product_title")
        ) and not tool_input.get("product_info"):
            return _reject("invalid_tool_input", "image_generation requires prompt or product information.")
    if tool_name == "product_search" and not (tool_input.get("query") or tool_input.get("visual_summary")):
        return _reject("invalid_tool_input", "product_search requires query or visual_summary.")
    if tool_name == "price_compare" and not (tool_input.get("query") or tool_input.get("items")):
        return _reject("invalid_tool_input", "price_compare requires query or items.")
    return None


def _has_explicit_render_intent(text: str, tool_input: dict[str, Any]) -> bool:
    combined = " ".join(str(value) for value in [text, *tool_input.values()] if value)
    lowered = combined.lower()
    strong = ("3d", "3D", "三维", "渲染", "建模", "模型", "场景预览", "展示空间", "创建场景")
    if any(keyword in combined for keyword in strong) or "3d" in lowered:
        return True
    if any(verb in combined for verb in ("放到", "放进", "放入")) and any(
        space in combined for space in ("客厅", "展厅", "办公室", "卧室", "空间", "场景")
    ):
        return True
    return False


def _reject(code: str, message: str) -> ActionValidationResult:
    return ActionValidationResult(accepted=False, code=code, message=message)
