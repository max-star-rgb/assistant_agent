"""Action validation for the assistant ReAct loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from assistant_agent.context.tool_exposure import (
    tool_exposure_facts,
    tool_media_requirements_satisfied,
)
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.tool_call_boundary import build_pre_tool_call_summary
from assistant_agent.tools.ids import (
    DURABLE_TASK_SUBMISSION_TOOL_NAMES,
)
from assistant_agent.tools.base import ToolInputValidationError
from assistant_agent.tools.input_binding import (
    bind_runtime_tool_input,
    llm_hidden_input_fields,
    runtime_bound_input_fields,
)
from assistant_agent.tools.registry import ToolRegistry


class ActionValidationResult(BaseModel):
    """Result of validating an assistant-proposed tool action."""

    accepted: bool
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    validated_input: BaseModel | None = Field(default=None, exclude=True)


class ActionValidator:
    """Validate tool actions before ToolExecutor runs them."""

    def validate(
        self,
        *,
        decision: AssistantToolCall,
        registry: ToolRegistry,
        request: UserRequest,
        state: AgentState,
    ) -> ActionValidationResult:
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
        run_tool_catalog = state.run_tool_catalog
        if run_tool_catalog is not None and not run_tool_catalog.allows(tool_name):
            return _reject(
                "tool_not_allowed_for_run",
                f"Tool is not enabled for the current assistant turn: {tool_name}.",
                metadata={
                    "run_tool_catalog": {
                        "schema_version": run_tool_catalog.schema_version,
                        "requested_tool_name": tool_name,
                        "available_tool_names": list(run_tool_catalog.available_tool_names),
                        "exclusion_reasons": list(
                            run_tool_catalog.excluded_reasons.get(tool_name, [])
                        ),
                    }
                },
            )
        media_error = _validate_required_media_scope(
            registry=registry,
            tool_name=tool_name,
            request=request,
        )
        if media_error is not None:
            return media_error
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
        unknown_fields = sorted(
            set(decision.tool_input) - set(tool.input_schema.model_fields)
        )
        if unknown_fields:
            return _reject(
                "invalid_tool_input",
                (
                    f"{tool_name} input contains unknown fields: "
                    f"{', '.join(unknown_fields)}."
                ),
                metadata=metadata,
            )
        supplied_runtime_fields = sorted(
            set(runtime_bound_input_fields(tool)).intersection(decision.tool_input)
        )
        if supplied_runtime_fields:
            return _reject(
                "runtime_owned_tool_input",
                (
                    f"{tool_name} input contains runtime-owned fields: "
                    f"{', '.join(supplied_runtime_fields)}."
                ),
                metadata=metadata,
            )
        supplied_tool_default_fields = sorted(
            set(llm_hidden_input_fields(tool)).intersection(decision.tool_input)
        )
        if supplied_tool_default_fields:
            return _reject(
                "tool_default_input_override",
                (
                    f"{tool_name} input attempts to override tool defaults: "
                    f"{', '.join(supplied_tool_default_fields)}."
                ),
                metadata=metadata,
            )
        try:
            complete_input = bind_runtime_tool_input(
                tool,
                decision.tool_input,
                state=state,
                step_id=decision.step_id or tool_name,
                context_metadata=request.metadata,
            )
            validated_input = tool.input_schema.model_validate(complete_input)
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

        return ActionValidationResult(
            accepted=True,
            code="accepted",
            message="Action accepted.",
            metadata=metadata,
            validated_input=validated_input,
        )


def _validate_task_execution_mode(
    *,
    tool_name: str,
    decision: AssistantToolCall,
    request: UserRequest,
) -> ActionValidationResult | None:
    mode = request.task_execution_mode
    binding = request.metadata.get("durable_task_binding")
    if mode == "foreground" and tool_name in DURABLE_TASK_SUBMISSION_TOOL_NAMES:
        return _reject(
            "durable_plan_forbidden",
            "Foreground execution does not allow durable task submission.",
        )
    if (
        mode == "durable"
        and binding is None
        and tool_name not in DURABLE_TASK_SUBMISSION_TOOL_NAMES
    ):
        return _reject(
            "durable_plan_required",
            "Durable execution requires an approved task-submission tool before business tools.",
        )
    if (
        mode != "durable"
        or binding is None
        or tool_name in DURABLE_TASK_SUBMISSION_TOOL_NAMES
    ):
        return None
    try:
        from assistant_agent.automation.durable_tasks.models import DurableTaskSnapshot, TrustedTaskBinding

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
    return None


def _validate_required_media(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_input: dict[str, Any],
    request: UserRequest,
) -> ActionValidationResult | None:
    required = set(registry.get_spec(tool_name).requires_media)
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


def _validate_required_media_scope(
    *,
    registry: ToolRegistry,
    tool_name: str,
    request: UserRequest,
) -> ActionValidationResult | None:
    spec = registry.get_spec(tool_name)
    if spec.media_scope == "any":
        return None
    if tool_media_requirements_satisfied(spec, tool_exposure_facts(request)):
        return None
    return _reject(
        "media_scope_not_available",
        f"{tool_name} is unavailable for the current media source.",
        metadata={"media_scope": spec.media_scope},
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
