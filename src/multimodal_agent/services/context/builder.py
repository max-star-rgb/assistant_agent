"""Build assistant context packs from runtime state."""

import json
from typing import Any

from multimodal_agent.agent.state import AgentState
from multimodal_agent.schemas.context import AssistantContextPack, AssistantPlanContext, ContextBudgetReport
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolSpec
from multimodal_agent.services.context.compaction import compact_observations_for_context


def build_assistant_context_pack(
    *,
    state: AgentState,
    request: UserRequest | None = None,
    observations: list[dict[str, Any]] | None = None,
    tool_specs: list[ToolSpec] | None = None,
    iteration: int,
    max_iterations: int,
    memory_summaries: list[str] | None = None,
    memory_text: str | None = None,
) -> AssistantContextPack:
    """Collect state and request materials for assistant prompt rendering."""

    active_request = request or state.request
    summaries = memory_summaries if memory_summaries is not None else [item.summary for item in state.memory_context]
    conversation_text = _conversation_context_text(active_request)
    text = (
        memory_text
        if memory_text is not None
        else _metadata_text(active_request, "memory_context_text") or "\n".join(summary for summary in summaries if summary)
    )
    memory_blocks = _metadata_dict_list(active_request, "memory_context_blocks")
    active_observations = observations or []
    context_observations = compact_observations_for_context(active_observations)
    active_tool_specs = tool_specs or []
    plan_state = build_assistant_plan_context(state)
    return AssistantContextPack(
        request=active_request,
        conversation_text=conversation_text,
        memory_summaries=summaries,
        memory_text=text,
        memory_blocks=memory_blocks,
        plan_state=plan_state,
        observations=context_observations,
        tool_specs=active_tool_specs,
        iteration=iteration,
        max_iterations=max_iterations,
        source_counts=_source_counts(
            request=active_request,
            memory_summaries=summaries,
            memory_blocks=memory_blocks,
            observations=active_observations,
            tool_specs=active_tool_specs,
        ),
        budget=_budget_report(
            request=active_request,
            conversation_text=conversation_text,
            memory_text=text,
            plan_state=plan_state,
            observations=context_observations,
            tool_specs=active_tool_specs,
        ),
    )


def build_assistant_plan_context(state: AgentState) -> AssistantPlanContext:
    """Render-safe snapshot of current plan-mode state."""

    return AssistantPlanContext(
        plan_mode_active=_is_plan_mode_active(state),
        plan_status=state.plan_status,
        current_step_id=state.current_step_id,
        plan_revision_count=state.plan_revision_count,
        current_plan=state.plan.model_dump(mode="json") if state.plan is not None else None,
    )


def _conversation_context_text(request: UserRequest) -> str:
    conversation_context = request.metadata.get("conversation_context_text")
    if isinstance(conversation_context, str) and conversation_context.strip():
        return conversation_context.strip()
    return ""


def _is_plan_mode_active(state: AgentState) -> bool:
    marker = state.request.metadata.get("plan_mode")
    return (
        isinstance(marker, dict)
        and marker.get("active") is True
        and state.plan is not None
        and state.plan_status in {"active", "replanning"}
    )


def _metadata_text(request: UserRequest, key: str) -> str:
    value = request.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _metadata_dict_list(request: UserRequest, key: str) -> list[dict[str, Any]]:
    value = request.metadata.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _source_counts(
    *,
    request: UserRequest,
    memory_summaries: list[str],
    memory_blocks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    tool_specs: list[ToolSpec],
) -> dict[str, int]:
    conversation_history = request.metadata.get("conversation_history")
    artifact_refs = request.metadata.get("memory_context_refs")
    return {
        "conversation_turns": len(conversation_history) if isinstance(conversation_history, list) else 0,
        "memory_items": len(memory_summaries),
        "memory_blocks": len(memory_blocks),
        "artifact_refs": len(artifact_refs) if isinstance(artifact_refs, list) else 0,
        "observations": len(observations),
        "tool_specs": len(tool_specs),
    }


def _budget_report(
    *,
    request: UserRequest,
    conversation_text: str,
    memory_text: str,
    plan_state: AssistantPlanContext,
    observations: list[dict[str, Any]],
    tool_specs: list[ToolSpec],
) -> ContextBudgetReport:
    request_chars = len(request.text or "")
    conversation_chars = len(conversation_text)
    memory_chars = len(memory_text)
    plan_chars = _json_chars(plan_state.model_dump(mode="json")) if _has_plan_context(plan_state) else 0
    observations_chars = _json_chars(observations)
    tool_spec_chars = _json_chars([spec.model_dump(mode="json") for spec in tool_specs])
    total_chars = (
        request_chars
        + conversation_chars
        + memory_chars
        + plan_chars
        + observations_chars
        + tool_spec_chars
    )
    return ContextBudgetReport(
        request_chars=request_chars,
        conversation_chars=conversation_chars,
        memory_chars=memory_chars,
        plan_chars=plan_chars,
        observations_chars=observations_chars,
        tool_spec_chars=tool_spec_chars,
        total_chars=total_chars,
    )


def _has_plan_context(plan_state: AssistantPlanContext) -> bool:
    return plan_state.current_plan is not None or plan_state.plan_status != "none"


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))
