"""Runtime-derived context helpers shared by native tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)


class ToolContext(BaseModel):
    """Execution context passed to legacy business hooks."""

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


def latest_human_request(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the latest user request and its trusted media references."""

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


def tool_context(
    runtime: ToolRuntime[AssistantRunContext],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> ToolContext:
    """Build the legacy business context from a native tool runtime."""

    execution = runtime.execution_info
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    request = latest_human_request(state)
    context_metadata = {
        "entry_profile": runtime.context.entry_profile,
        "visual_target_sequence": request.get("visual_target_sequence"),
    }
    if metadata is not None:
        context_metadata.update(metadata)
    return ToolContext(
        user_id=authenticated_user_identity(runtime),
        session_id=getattr(execution, "thread_id", None),
        run_id=getattr(execution, "run_id", None),
        metadata=context_metadata,
        skill_reference_grants=skill_reference_grants(state),
    )


def skill_reference_grants(state: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return validated skill-to-reference grants from graph state."""

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
