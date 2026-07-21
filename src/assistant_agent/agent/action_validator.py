"""Action validation for the assistant ReAct loop."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.tool_call_boundary import build_pre_tool_call_summary
from assistant_agent.services.tool_manifest import TASK_PLAN_SUBMIT_TOOL_NAME
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.base import ToolInputValidationError
from assistant_agent.tools.registry import ToolRegistry


class ActionValidationResult(BaseModel):
    """Result of validating an assistant-proposed tool action."""

    accepted: bool
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
        task_mode_error = _validate_task_execution_mode(
            tool_name=tool_name,
            decision=decision,
            request=request,
        )
        if task_mode_error is not None:
            return task_mode_error
        run_tool_set = state.run_tool_set
        if run_tool_set is not None and not run_tool_set.allows_execution(tool_name):
            return _reject(
                "tool_not_allowed_for_run",
                f"Tool is not enabled for the current assistant turn: {tool_name}.",
                metadata={
                    "run_tool_set": {
                        "schema_version": run_tool_set.schema_version,
                        "requested_tool_name": tool_name,
                        "executable_tool_names": list(run_tool_set.executable_tool_names),
                        "exclusion_reasons": list(
                            run_tool_set.excluded_reasons.get(tool_name, [])
                        ),
                    }
                },
            )
        metadata = {
            "pre_tool_call": build_pre_tool_call_summary(
                tool_name=tool_name,
                tool_input=decision.tool_input,
                registry=registry,
                request=request,
                state=state,
                step_id=decision.step_id,
            )
        }

        tool = registry.get(tool_name)
        try:
            validated_input = tool.input_schema.model_validate(decision.tool_input)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"msg": "invalid input"}
            return _reject(
                "invalid_tool_input",
                f"{tool_name} input invalid: {first.get('msg', 'invalid input')}",
                metadata=metadata,
            )

        media_error = _validate_required_media(
            registry=registry,
            tool_name=tool_name,
            tool_input=validated_input.model_dump(mode="python"),
            request=request,
        )
        if media_error is not None:
            return _with_metadata(media_error, metadata)

        validate_call = getattr(tool, "validate_call", None)
        if callable(validate_call):
            try:
                validate_call(validated_input)
            except ToolInputValidationError as exc:
                return _reject(exc.code, exc.message, metadata=metadata)

        return ActionValidationResult(accepted=True, code="accepted", message="Action accepted.", metadata=metadata)


def _validate_task_execution_mode(
    *,
    tool_name: str,
    decision: AssistantDecision,
    request: UserRequest,
) -> ActionValidationResult | None:
    mode = request.task_execution_mode
    binding = request.metadata.get("durable_task_binding")
    if mode == "foreground" and tool_name == TASK_PLAN_SUBMIT_TOOL_NAME:
        return _reject(
            "durable_plan_forbidden",
            "Foreground execution does not allow durable task submission.",
        )
    if mode == "durable" and binding is None and tool_name != TASK_PLAN_SUBMIT_TOOL_NAME:
        return _reject(
            "durable_plan_required",
            "Durable execution requires task_plan_submit before business tools.",
        )
    if mode != "durable" or binding is None or tool_name == TASK_PLAN_SUBMIT_TOOL_NAME:
        return None
    try:
        from assistant_agent.schemas.durable_tasks import DurableTaskSnapshot, TrustedTaskBinding

        trusted_binding = TrustedTaskBinding.model_validate(binding)
        snapshot = DurableTaskSnapshot.model_validate(
            request.metadata.get("durable_task_snapshot")
        )
    except (TypeError, ValueError):
        return _reject("durable_task_binding_invalid", "Durable task binding is invalid.")
    if decision.step_id not in trusted_binding.ready_step_ids:
        return _reject("durable_step_not_ready", "Tool call does not target a ready durable step.")
    step = next((item for item in snapshot.plan.steps if item.step_id == decision.step_id), None)
    if step is None or step.tool_name != tool_name:
        return _reject("durable_step_tool_mismatch", "Tool call does not match the bound durable step.")
    if trusted_binding.verified_confirmation_id is not None:
        digest = hashlib.sha256(
            json.dumps(
                decision.tool_input or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if (
            trusted_binding.verified_confirmation_tool_name != tool_name
            or trusted_binding.verified_confirmation_input_digest != digest
        ):
            return _reject(
                "durable_confirmation_binding_mismatch",
                "Tool call does not match the user-approved durable action.",
            )
    return None


def _validate_required_media(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_input: dict[str, Any],
    request: UserRequest,
) -> ActionValidationResult | None:
    required = set(
        ToolPolicyInterpreter()
        .view_for_spec(registry.get_spec(tool_name))
        .requires_media
    )
    if not required:
        return None
    available = _available_media_types(tool_input, request)
    if required.intersection(available):
        return None
    expected = ", ".join(sorted(required))
    return _reject(
        "missing_required_input",
        f"{tool_name} requires one of these media types: {expected}.",
    )


def _available_media_types(
    tool_input: dict[str, Any], request: UserRequest
) -> set[str]:
    available: set[str] = set()
    if tool_input.get("image_url") or tool_input.get("image_ids") or request.image_ids:
        available.add("image")
    if tool_input.get("video_ref") or tool_input.get("video_ids") or request.video_ids:
        available.add("video")
    if tool_input.get("audio_id") or request.audio_id:
        available.add("audio")
    return available


def _reject(code: str, message: str, *, metadata: dict[str, Any] | None = None) -> ActionValidationResult:
    return ActionValidationResult(accepted=False, code=code, message=message, metadata=metadata or {})


def _with_metadata(result: ActionValidationResult, metadata: dict[str, Any]) -> ActionValidationResult:
    return result.model_copy(update={"metadata": {**result.metadata, **metadata}}, deep=True)
