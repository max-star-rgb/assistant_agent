"""Action validation for the assistant ReAct loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from assistant_agent.agent.state import AgentState
from assistant_agent.memory.read_policy import MemoryReadPolicy
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.tool_call_boundary import build_pre_tool_call_summary
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

        media_error = _validate_required_media(tool_name, decision.tool_input, request)
        if media_error is not None:
            return _with_metadata(media_error, metadata)

        semantic_error = _validate_required_semantic_inputs(tool_name, decision.tool_input)
        if semantic_error is not None:
            return _with_metadata(semantic_error, metadata)

        memory_read_decision = _memory_read_decision(tool_name, decision.tool_input, request)
        if memory_read_decision is not None:
            metadata["memory_read_policy"] = memory_read_decision.prompt_safe_metadata()
            if not memory_read_decision.allowed:
                return _reject(
                    "memory_read_intent_required",
                    "memory retrieval requires an explicit request for prior chats, saved memory, previous context, or remembered preferences.",
                    metadata=metadata,
                )

        if tool_name == "render_3d" and not _has_explicit_render_intent(request.text or "", decision.tool_input):
            return _reject(
                "render_intent_required",
                "render_3d requires explicit 3D, render, modeling, or scene-preview intent.",
                metadata=metadata,
            )
        if tool_name == "memory_media_ingest" and not _has_memory_media_ingest_intent(
            request.text or "",
            decision.tool_input,
        ):
            return _reject(
                "memory_media_ingest_intent_required",
                "memory_media_ingest requires explicit media ingestion into memory intent.",
                metadata=metadata,
            )

        try:
            registry.get(tool_name).input_schema.model_validate(decision.tool_input)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"msg": "invalid input"}
            return _reject(
                "invalid_tool_input",
                f"{tool_name} input invalid: {first.get('msg', 'invalid input')}",
                metadata=metadata,
            )

        return ActionValidationResult(accepted=True, code="accepted", message="Action accepted.", metadata=metadata)


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
    if tool_name == "web_search" and not _non_empty_string(tool_input.get("query")):
        return _reject("invalid_tool_input", "web_search requires query.")
    if tool_name == "memory_retrieval" and not tool_input.get("query"):
        return _reject("invalid_tool_input", "memory_retrieval requires query.")
    if tool_name == "memory":
        memory_error = _validate_legacy_memory_tool_input(tool_input)
        if memory_error is not None:
            return memory_error
    if tool_name == "memory_save" and not _has_memory_save_text(tool_input):
        return _reject("invalid_tool_input", "memory_save requires query, content.text, or content.summary.")
    if tool_name == "memory_save":
        source_error = _validate_memory_save_source(tool_input)
        if source_error is not None:
            return source_error
    if tool_name == "memory_ingest_status" and not _non_empty_string(tool_input.get("task_id")):
        return _reject("invalid_tool_input", "memory_ingest_status requires task_id.")
    if tool_name == "delegate_to_agent":
        if not tool_input.get("target_agent_id"):
            return _reject("invalid_tool_input", "delegate_to_agent requires target_agent_id.")
        if not _has_agent_delegation_payload(tool_input):
            return _reject("invalid_tool_input", "delegate_to_agent requires text, image_ids, video_ids, or audio_id.")
    return None


def _has_memory_save_text(tool_input: dict[str, Any]) -> bool:
    if tool_input.get("query"):
        return True
    content = tool_input.get("content")
    if not isinstance(content, dict):
        return False
    return bool(content.get("text") or content.get("summary"))


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_legacy_memory_tool_input(tool_input: dict[str, Any]) -> ActionValidationResult | None:
    action = tool_input.get("action")
    if action == "retrieve":
        if not tool_input.get("query"):
            return _reject("invalid_tool_input", "memory retrieve requires query.")
        return None
    if action == "save":
        if not _has_memory_save_text(tool_input):
            return _reject("invalid_tool_input", "memory save requires query, content.text, or content.summary.")
        return _validate_memory_save_source(tool_input)
    return _reject("invalid_tool_input", "memory tool requires action=retrieve or action=save.")


def _validate_memory_save_source(tool_input: dict[str, Any]) -> ActionValidationResult | None:
    source_intent = tool_input.get("source_intent")
    if not isinstance(source_intent, str) or not source_intent.strip():
        return _reject("invalid_tool_input", "assistant-loop memory_save requires source_intent.")
    if source_intent not in {"user_explicit", "assistant_candidate", "user_confirmed"}:
        return _reject("invalid_tool_input", "memory_save source_intent is invalid.")
    if source_intent == "user_confirmed":
        return _reject("invalid_tool_input", "memory_save source_intent=user_confirmed is reserved for confirmation service.")
    for key in ("source_reason", "future_use", "evidence"):
        if not isinstance(tool_input.get(key), str) or not tool_input[key].strip():
            return _reject("invalid_tool_input", f"assistant-loop memory_save requires {key}.")
    return None


def _memory_read_decision(
    tool_name: str,
    tool_input: dict[str, Any],
    request: UserRequest,
):
    if tool_name == "memory_retrieval":
        query = str(tool_input.get("query") or "")
    elif tool_name == "memory" and tool_input.get("action") == "retrieve":
        query = str(tool_input.get("query") or "")
    else:
        return None
    content = tool_input.get("content")
    content = content if isinstance(content, dict) else {}
    return MemoryReadPolicy().decide_tool_retrieval(
        request_text=request.text or "",
        query_text=query,
        metadata=request.metadata,
        top_k=content.get("top_k") if isinstance(content.get("top_k"), int) else None,
        max_context_chars=content.get("max_context_chars")
        if isinstance(content.get("max_context_chars"), int)
        else None,
    )


def _has_agent_delegation_payload(tool_input: dict[str, Any]) -> bool:
    return bool(
        tool_input.get("text")
        or tool_input.get("image_ids")
        or tool_input.get("video_ids")
        or tool_input.get("audio_id")
    )


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


def _has_memory_media_ingest_intent(text: str, tool_input: dict[str, Any]) -> bool:
    combined = " ".join(str(value) for value in [text, *tool_input.values()] if value)
    lowered = combined.lower()
    has_memory_target = any(
        keyword in combined
        for keyword in ("记忆", "长期记忆", "记忆服务", "Memory Server", "memory server")
    ) or "memory" in lowered
    has_ingest_action = any(
        keyword in combined
        for keyword in ("上传", "导入", "摄入", "入库", "保存到", "写入")
    ) or any(keyword in lowered for keyword in ("upload", "ingest", "import"))
    has_media = any(keyword in combined for keyword in ("视频", "图片", "音频", "媒体")) or any(
        keyword in lowered for keyword in ("video", "image", "audio", "media")
    )
    return has_memory_target and has_ingest_action and has_media


def _reject(code: str, message: str, *, metadata: dict[str, Any] | None = None) -> ActionValidationResult:
    return ActionValidationResult(accepted=False, code=code, message=message, metadata=metadata or {})


def _with_metadata(result: ActionValidationResult, metadata: dict[str, Any]) -> ActionValidationResult:
    return result.model_copy(update={"metadata": {**result.metadata, **metadata}}, deep=True)
