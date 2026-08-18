"""Base contracts for governed LangChain-native tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, ToolException
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.tools.input_binding import (
    RuntimeInputBinding,
    llm_forbidden_input_fields,
    validate_tool_input_contract,
)
from assistant_agent.tools.models import (
    ToolCategory,
    ToolMediaRequirement,
    ToolMediaScope,
    ToolRepeatPolicy,
    ToolResult,
)


class ToolInputValidationError(ValueError):
    """Stable tool-owned rejection raised before a Tool runs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolContext(BaseModel):
    """Execution context passed to tools."""

    run_id: str | None = None
    trace_id: str | None = None
    trace_store: Any | None = Field(default=None, exclude=True)
    parent_span_id: str | None = Field(default=None, exclude=True)
    user_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    skill_reference_grants: dict[str, list[str]] = Field(
        default_factory=dict,
        exclude=True,
    )
    cancel_token: Any | None = Field(default=None, exclude=True)

    def is_cancelled(self) -> bool:
        """Return whether the current run has requested cooperative cancellation."""

        checker = getattr(self.cancel_token, "is_cancelled", None)
        if callable(checker):
            return bool(checker())
        is_set = getattr(self.cancel_token, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        cancelled = getattr(self.cancel_token, "cancelled", None)
        return bool(cancelled) if isinstance(cancelled, bool) else False


class ToolBase(BaseTool):
    """Direct LangChain Tool base with the project's governed result contract.

    Production built-ins implement the small synchronous ``_execute(input,
    context)`` business hook directly. Composition never wraps concrete tools
    in a second ``StructuredTool``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]
    output_schema: ClassVar[type[BaseModel]]
    category: ClassVar[ToolCategory] = "dangerous"
    requires_media: ClassVar[list[ToolMediaRequirement]] = []
    media_scope: ClassVar[ToolMediaScope] = "any"
    repeat_policy: ClassVar[ToolRepeatPolicy] = "once_per_run"
    llm_hidden_input_fields: ClassVar[tuple[str, ...]] = ()
    runtime_input_bindings: ClassVar[tuple[Any, ...]] = ()
    trace_content_policy: ClassVar[Literal["default", "metadata_only"]] = "default"

    def __init__(self) -> None:
        validate_tool_input_contract(self)
        super().__init__(
            args_schema=_native_input_model(self),
            response_format="content_and_artifact",
            metadata={"effect": self.category, "source": "builtin"},
        )

    def _run(
        self,
        runtime: ToolRuntime[AssistantRunContext],
        **payload: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._invoke_native(payload, runtime)

    async def _arun(
        self,
        runtime: ToolRuntime[AssistantRunContext],
        **payload: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await asyncio.to_thread(self._invoke_native, payload, runtime)

    def _invoke_native(
        self,
        payload: Mapping[str, Any],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            bound = _bind_native_input(self, payload, runtime)
            validated = self.input_schema.model_validate(bound)
            result = self._execute(validated, _tool_context(runtime))
        except ValidationError as exc:
            raise ToolException(f"Invalid input: {exc.errors()[0]['msg']}") from exc
        except ToolException:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise ToolException(sanitize_error_message(exc)) from exc
        if not result.success:
            raise ToolException(result.error or f"{self.name} failed")
        observation = result.model_observation
        if observation is None:
            observation = result.data or {"status": "succeeded"}
        return observation, dict(result.data or {})

    def _execute(self, input: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError


def _native_input_model(tool: ToolBase) -> type[BaseModel]:
    forbidden = set(llm_forbidden_input_fields(tool))
    fields = {
        name: (field.annotation, field)
        for name, field in tool.input_schema.model_fields.items()
        if name not in forbidden
    }
    fields["runtime"] = (ToolRuntime[AssistantRunContext], ...)
    return create_model(
        f"{type(tool).__name__}NativeInput",
        __config__=ConfigDict(
            arbitrary_types_allowed=True,
            extra="forbid",
            strict=True,
        ),
        **fields,
    )


def _bind_native_input(
    tool: ToolBase,
    payload: Mapping[str, Any],
    runtime: ToolRuntime[AssistantRunContext],
) -> dict[str, Any]:
    bound = dict(payload)
    for raw in tool.runtime_input_bindings:
        binding = (
            raw
            if isinstance(raw, RuntimeInputBinding)
            else RuntimeInputBinding.model_validate(raw)
        )
        value = _binding_value(binding, runtime)
        if value is not _MISSING:
            bound[binding.field] = value
    return bound


def _binding_value(
    binding: RuntimeInputBinding,
    runtime: ToolRuntime[AssistantRunContext],
) -> Any:
    execution = runtime.execution_info
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    if binding.source == "runtime_identity":
        identity = {
            "user_id": authenticated_user_identity(runtime),
            "session_id": getattr(execution, "thread_id", None),
            "run_id": getattr(execution, "run_id", None),
        }
        return identity.get(binding.key or "", _MISSING)
    if binding.source == "memory_context":
        memories = tuple(state.get("memory_context", ()))
        if binding.key == "summaries":
            return list(memories)
        if binding.key == "text":
            return "\n".join(memories)
    if binding.source == "request":
        return _latest_human_request(state).get(binding.key or "", _MISSING)
    if binding.source == "durable_idempotency":
        thread_id = getattr(execution, "thread_id", "") or "thread"
        return f"native:{thread_id}:{runtime.tool_call_id or 'tool-call'}"
    return _MISSING


def _latest_human_request(state: Mapping[str, Any]) -> dict[str, Any]:
    for message in reversed(state.get("messages", ())):
        if not isinstance(message, HumanMessage):
            continue
        result: dict[str, Any] = {"image_ids": [], "video_ids": []}
        if isinstance(message.content, str):
            result["text"] = message.content
            return result
        texts: list[str] = []
        for block in message.content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block_type in {"image", "image_url"} and block.get("id"):
                result["image_ids"].append(str(block["id"]))
            elif block_type in {"video", "file"} and block.get("id"):
                result["video_ids"].append(str(block["id"]))
                target_sequence = block.get("target_sequence")
                if (
                    not isinstance(target_sequence, bool)
                    and isinstance(target_sequence, int)
                    and target_sequence >= 0
                ):
                    result["visual_target_sequence"] = target_sequence
        result["text"] = "\n".join(texts)
        return result
    return {}


def _tool_context(runtime: ToolRuntime[AssistantRunContext]) -> ToolContext:
    execution = runtime.execution_info
    request = _latest_human_request(
        runtime.state if isinstance(runtime.state, Mapping) else {}
    )
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    return ToolContext(
        user_id=authenticated_user_identity(runtime),
        session_id=getattr(execution, "thread_id", None),
        run_id=getattr(execution, "run_id", None),
        metadata={
            "entry_profile": runtime.context.entry_profile,
            "visual_target_sequence": request.get("visual_target_sequence"),
        },
        skill_reference_grants=_skill_reference_grants(state),
    )


def _skill_reference_grants(state: Mapping[str, Any]) -> dict[str, list[str]]:
    raw = state.get("skill_reference_grants")
    if not isinstance(raw, Mapping):
        return {}
    return {
        skill_id: [
            reference_id
            for reference_id in reference_ids
            if isinstance(reference_id, str) and reference_id
        ]
        for skill_id, reference_ids in raw.items()
        if isinstance(skill_id, str)
        and skill_id
        and isinstance(reference_ids, (list, tuple))
    }


_MISSING = object()
