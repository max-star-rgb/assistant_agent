"""Generic runtime-owned input binding for governed tools."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from assistant_agent.automation.durable_tasks.models import TrustedTaskBinding

if TYPE_CHECKING:
    from assistant_agent.runtime.state import AgentState


ToolInputBindingSource = Literal[
    "constant",
    "runtime_identity",
    "request",
    "memory_context",
    "latest_tool_result",
    "durable_idempotency",
]
ToolInputBindingMode = Literal["always", "if_missing"]


class ToolInputBinding(BaseModel):
    """Declare one model-hidden field supplied outside model tool arguments."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    source: ToolInputBindingSource
    key: str | None = None
    value: Any = None
    result_tool_name: str | None = None
    result_path: str | None = None
    mode: ToolInputBindingMode = "always"

    @model_validator(mode="after")
    def validate_source_options(self) -> "ToolInputBinding":
        if self.source in {"runtime_identity", "request", "memory_context"} and not self.key:
            raise ValueError(f"{self.source} binding requires key")
        if self.source == "latest_tool_result" and not self.result_path:
            raise ValueError("latest_tool_result binding requires result_path")
        return self


def runtime_owned_input_fields(tool: Any) -> tuple[str, ...]:
    """Return every field that must not be supplied by the model."""

    fields = list(getattr(tool, "model_hidden_input_fields", ()))
    fields.extend(binding.field for binding in _runtime_input_bindings(tool))
    return tuple(dict.fromkeys(fields))


def validate_runtime_input_bindings(tool: Any) -> None:
    """Fail startup when a Tool declares an invalid runtime binding contract."""

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
    for binding in bindings:
        if binding.source != "constant":
            continue
        annotation = tool.input_schema.model_fields[binding.field].annotation
        TypeAdapter(annotation).validate_python(binding.value)


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
    disallowed = set(trusted) - set(runtime_owned_input_fields(tool))
    if disallowed:
        names = ", ".join(sorted(disallowed))
        raise ValueError(f"runtime_input contains model-owned fields: {names}")
    bound.update(trusted)
    return bound


def _runtime_input_bindings(tool: Any) -> tuple[ToolInputBinding, ...]:
    raw = getattr(tool, "runtime_input_bindings", ())
    return tuple(
        item if isinstance(item, ToolInputBinding) else ToolInputBinding.model_validate(item)
        for item in raw
    )


def _resolve_binding(
    binding: ToolInputBinding,
    *,
    state: AgentState,
    step_id: str,
    context_metadata: dict[str, Any],
) -> Any:
    if binding.source == "constant":
        return deepcopy(binding.value)
    if binding.source == "runtime_identity":
        return getattr(state, binding.key or "", _UNRESOLVED)
    if binding.source == "request":
        return getattr(state.request, binding.key or "", _UNRESOLVED)
    if binding.source == "memory_context":
        memories = (
            state.session_memory_snapshot.memories
            if state.session_memory_snapshot is not None
            else []
        )
        if binding.key == "summaries":
            return [
                memory.text for memory in memories if memory.text
            ]
        if binding.key == "text":
            return "\n".join(
                memory.text for memory in memories if memory.text
            )
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
