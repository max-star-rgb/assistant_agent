"""Generic runtime-owned input binding for governed tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.automation.durable_tasks.models import TrustedTaskBinding

if TYPE_CHECKING:
    from assistant_agent.runtime.state import AgentState


RuntimeInputBindingSource = Literal[
    "runtime_identity",
    "request",
    "memory_context",
    "latest_tool_result",
    "durable_idempotency",
    "runtime_input",
]
RuntimeInputBindingMode = Literal["always", "if_missing"]


class RuntimeInputBinding(BaseModel):
    """Declare one model-hidden field supplied outside model tool arguments."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    source: RuntimeInputBindingSource
    key: str | None = None
    result_tool_name: str | None = None
    result_path: str | None = None
    mode: RuntimeInputBindingMode = "always"

    @model_validator(mode="after")
    def validate_source_options(self) -> "RuntimeInputBinding":
        if self.source in {"runtime_identity", "request", "memory_context"} and not self.key:
            raise ValueError(f"{self.source} binding requires key")
        if self.source == "latest_tool_result" and not self.result_path:
            raise ValueError("latest_tool_result binding requires result_path")
        return self


def runtime_bound_input_fields(tool: Any) -> tuple[str, ...]:
    """Return fields whose values come from trusted runtime state."""

    return tuple(binding.field for binding in _runtime_input_bindings(tool))


def llm_hidden_input_fields(tool: Any) -> tuple[str, ...]:
    """Return tool-default fields intentionally omitted from the LLM schema."""

    return tuple(dict.fromkeys(getattr(tool, "llm_hidden_input_fields", ())))


def llm_forbidden_input_fields(tool: Any) -> tuple[str, ...]:
    """Return all input fields that the LLM is not allowed to submit."""

    return tuple(
        dict.fromkeys(
            (*runtime_bound_input_fields(tool), *llm_hidden_input_fields(tool))
        )
    )


def validate_tool_input_contract(tool: Any) -> None:
    """Fail startup when a Tool declares inconsistent input ownership."""

    bindings = _runtime_input_bindings(tool)
    field_names = set(tool.input_schema.model_fields)
    declared = [binding.field for binding in bindings]
    unknown = sorted(set(declared) - field_names)
    if unknown:
        raise ValueError(
            f"{tool.name} runtime input bindings reference unknown fields: "
            f"{', '.join(unknown)}"
        )
    duplicates = sorted({field for field in declared if declared.count(field) > 1})
    if duplicates:
        raise ValueError(
            f"{tool.name} runtime input bindings contain duplicate fields: "
            f"{', '.join(duplicates)}"
        )
    hidden = list(llm_hidden_input_fields(tool))
    unknown_hidden = sorted(set(hidden) - field_names)
    if unknown_hidden:
        raise ValueError(
            f"{tool.name} llm hidden inputs reference unknown fields: "
            f"{', '.join(unknown_hidden)}"
        )
    overlap = sorted(set(declared).intersection(hidden))
    if overlap:
        raise ValueError(
            f"{tool.name} input fields cannot be both runtime-bound and "
            f"LLM-hidden defaults: {', '.join(overlap)}"
        )
    required_hidden = sorted(
        field
        for field in hidden
        if tool.input_schema.model_fields[field].is_required()
    )
    if required_hidden:
        raise ValueError(
            f"{tool.name} llm hidden inputs require Pydantic defaults: "
            f"{', '.join(required_hidden)}"
        )


def bind_runtime_tool_input(
    tool: Any,
    model_input: dict[str, Any],
    *,
    state: AgentState,
    step_id: str,
    context_metadata: dict[str, Any],
    runtime_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve declared bindings, then merge explicitly trusted runtime input."""

    bound = dict(model_input)
    for binding in _runtime_input_bindings(tool):
        if binding.mode == "if_missing" and _has_value(bound.get(binding.field)):
            continue
        resolved = _resolve_binding(
            binding,
            state=state,
            step_id=step_id,
            context_metadata=context_metadata,
        )
        if resolved is not _UNRESOLVED:
            bound[binding.field] = resolved

    trusted = dict(runtime_input or {})
    disallowed = set(trusted) - set(runtime_bound_input_fields(tool))
    if disallowed:
        names = ", ".join(sorted(disallowed))
        raise ValueError(f"runtime_input contains model-owned fields: {names}")
    bound.update(trusted)
    return bound


def _runtime_input_bindings(tool: Any) -> tuple[RuntimeInputBinding, ...]:
    raw = getattr(tool, "runtime_input_bindings", ())
    return tuple(
        item
        if isinstance(item, RuntimeInputBinding)
        else RuntimeInputBinding.model_validate(item)
        for item in raw
    )


def _resolve_binding(
    binding: RuntimeInputBinding,
    *,
    state: AgentState,
    step_id: str,
    context_metadata: dict[str, Any],
) -> Any:
    if binding.source == "runtime_identity":
        return getattr(state, binding.key or "", _UNRESOLVED)
    if binding.source == "request":
        return getattr(state.request, binding.key or "", _UNRESOLVED)
    if binding.source == "memory_context":
        if binding.key == "summaries":
            return list(state.memory_texts)
        if binding.key == "text":
            return "\n".join(state.memory_texts)
        return _UNRESOLVED
    if binding.source == "latest_tool_result":
        return _latest_tool_result_value(
            state,
            tool_name=binding.result_tool_name,
            path=binding.result_path or "",
        )
    if binding.source == "durable_idempotency":
        raw = context_metadata.get("durable_task_binding")
        if raw is None:
            return _UNRESOLVED
        task_binding = TrustedTaskBinding.model_validate(raw)
        return task_binding.step_idempotency_keys.get(step_id, _UNRESOLVED)
    if binding.source == "runtime_input":
        return _UNRESOLVED
    return _UNRESOLVED


def _latest_tool_result_value(
    state: AgentState,
    *,
    tool_name: str | None,
    path: str,
) -> Any:
    for result in reversed(state.tool_results):
        if not result.success or (tool_name and result.tool_name != tool_name):
            continue
        value: Any = result.data or result.model_observation or {}
        for part in path.split("."):
            if not part:
                continue
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                value = _UNRESOLVED
                break
        if value is not _UNRESOLVED:
            return value
    return _UNRESOLVED


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


_UNRESOLVED = object()
