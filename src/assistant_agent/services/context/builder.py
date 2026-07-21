"""Build assistant context packs from runtime state."""

import json
from dataclasses import dataclass
from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.context import (
    AssistantContextPack,
    AssistantPlanContext,
    ContextBudgetReport,
    ContextPolicy,
    ContextSection,
    ContextSummary,
    RealtimeVideoContext,
    ToolCapabilityDescriptor,
)
from assistant_agent.schemas.durable_tasks import DurableTaskSnapshot
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec
from assistant_agent.services.context.compactor import (
    ContextCompactor,
    DeterministicContextCompactor,
    context_summary_from_metadata,
    format_context_summary,
)
from assistant_agent.services.context.compaction import compact_observations_for_context
from assistant_agent.services.context.conversation import select_conversation_window
from assistant_agent.services.context.policy import (
    COMPRESSION_REASON_CONTEXT_BUDGET_TRIMMED,
    COMPRESSION_REASON_CONTEXT_OVER_BUDGET,
    COMPRESSION_REASON_CONVERSATION_COMPACTED,
    COMPRESSION_REASON_OBSERVATION_COMPACTED,
    COMPRESSION_STAGE_BUDGET_TRIMMED,
    COMPRESSION_STAGE_COMPACTED,
    COMPRESSION_STAGE_NONE,
    CONTEXT_BUDGET_METADATA_KEY,
    CompactionPolicy,
    context_policy_from_request,
)
from assistant_agent.services.context.capability_catalog import (
    select_tool_capability_descriptors,
)
from assistant_agent.services.context.report import build_context_source_report
from assistant_agent.services.context.token_budget import token_budget_reporter_from_request
from assistant_agent.services.context.tool_catalog import prompt_tool_spec_payload, select_prompt_tool_specs
from assistant_agent.services.realtime_task_state import REALTIME_TASK_STATE_METADATA_KEY


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
    context_compactor: ContextCompactor | None = None,
) -> AssistantContextPack:
    """Collect state and request materials for assistant prompt rendering."""

    active_request = request or state.request
    context_policy = context_policy_from_request(active_request)
    summaries = memory_summaries if memory_summaries is not None else [item.summary for item in state.memory_context]
    conversation_text = _conversation_context_text(active_request)
    context_summary = _context_summary(active_request)
    compactor_type = _metadata_text(active_request, "context_compactor_type") or "none"
    text = (
        memory_text
        if memory_text is not None
        else _metadata_text(active_request, "memory_context_text") or "\n".join(summary for summary in summaries if summary)
    )
    memory_blocks = _metadata_dict_list(active_request, "memory_context_blocks")
    realtime_task_state = _metadata_dict(active_request, REALTIME_TASK_STATE_METADATA_KEY)
    realtime_video_context = _realtime_video_context(active_request)
    durable_task_state, durable_task_state_trimmed = _durable_task_context(active_request)
    active_observations = observations or []
    context_observations = compact_observations_for_context(active_observations)
    active_tool_specs = tool_specs or []
    tool_catalog = select_prompt_tool_specs(active_request, active_tool_specs)
    prompt_tool_specs = tool_catalog.available_tool_specs
    state.run_tool_catalog = tool_catalog.run_tool_catalog
    tool_capability_catalog = select_tool_capability_descriptors(
        request=active_request,
        qualified_tool_specs=tool_catalog.available_tool_specs,
        prompt_tool_specs=prompt_tool_specs,
        tool_catalog_summary=tool_catalog.summary,
        active_skill_ids=set(tool_catalog.active_skill_ids),
    )
    tool_capabilities = tool_capability_catalog.capabilities
    budget_limit = _effective_context_budget_limit(
        request=active_request,
        base_max_chars=context_policy.max_context_chars,
        tool_specs=prompt_tool_specs,
        tool_capabilities=tool_capabilities,
    )
    plan_state = build_assistant_plan_context(state)
    unbudgeted_context_sections = [
        section
        for section in state.context_source_result.sections
        if section.kind == "soul" and not section.sensitive
    ]
    unbudgeted_report = _budget_report(
        request=active_request,
        conversation_text=conversation_text,
        memory_text=text,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        plan_state=plan_state,
        observations=context_observations,
        tool_specs=prompt_tool_specs,
        tool_capabilities=tool_capabilities,
        context_sections=unbudgeted_context_sections,
        max_chars=budget_limit,
    )
    context_sections, context_section_trimmed = _fit_context_sections_to_budget(
        unbudgeted_context_sections,
        available_chars=max(
            0,
            budget_limit
            - (unbudgeted_report.total_chars - unbudgeted_report.owner_persona_chars),
        ),
    )
    source_counts = _source_counts(
        request=active_request,
        memory_summaries=summaries,
        memory_blocks=memory_blocks,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        observations=active_observations,
        tool_specs=active_tool_specs,
        prompt_tool_specs=prompt_tool_specs,
        tool_capabilities=tool_capabilities,
        context_sections=context_sections,
        context_source_issue_count=len(state.context_source_result.issues),
    )
    initial_budget = _budget_report(
        request=active_request,
        conversation_text=conversation_text,
        memory_text=text,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        plan_state=plan_state,
        observations=context_observations,
        tool_specs=prompt_tool_specs,
        tool_capabilities=tool_capabilities,
        context_sections=context_sections,
        max_chars=budget_limit,
    )
    compaction_budget_policy = context_policy.model_copy(
        update={"max_context_chars": budget_limit}
    )
    compaction_decision = CompactionPolicy().evaluate(
        request=active_request,
        budget=initial_budget,
        observations=context_observations,
        policy=compaction_budget_policy,
    )
    if compaction_decision.triggered:
        compactor = context_compactor or DeterministicContextCompactor()
        compaction = compactor.compact(
            conversation=_conversation_turns_to_compact(active_request, context_policy=context_policy),
            current_request=active_request,
            observations=context_observations,
            budget_report=initial_budget,
            existing_summary=context_summary,
        )
        context_summary = compaction.summary
        compactor_type = compaction.compactor_type
        active_request.metadata["context_summary"] = context_summary.model_dump(mode="json")
        active_request.metadata["context_summary_text"] = format_context_summary(context_summary)
        active_request.metadata["context_summary_present"] = True
        active_request.metadata["context_compactor_type"] = compactor_type

    owner_persona_chars = _owner_persona_chars(context_sections)
    budgeted = _enforce_context_budget(
        request=active_request,
        conversation_text=conversation_text,
        memory_text=text,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        observations=context_observations,
        plan_state=plan_state,
        tool_specs=prompt_tool_specs,
        tool_capabilities=tool_capabilities,
        max_chars=max(0, budget_limit - owner_persona_chars),
    )
    if budgeted.memory_text == "":
        summaries = []
    trimmed_sections = _unique(
        [
            *context_section_trimmed,
            *(["durable_task_state"] if durable_task_state_trimmed else []),
            *budgeted.trimmed_sections,
        ]
    )
    over_budget = unbudgeted_report.total_chars > budget_limit
    compression_reasons = _compression_reasons(
        request=active_request,
        observations=context_observations,
        over_budget=over_budget,
        trimmed_sections=trimmed_sections,
        extra_reasons=compaction_decision.reasons,
    )
    final_budget = _budget_report(
        request=active_request,
        conversation_text=budgeted.conversation_text,
        memory_text=budgeted.memory_text,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        plan_state=plan_state,
        observations=budgeted.observations,
        tool_specs=prompt_tool_specs,
        tool_capabilities=tool_capabilities,
        context_sections=context_sections,
        max_chars=budget_limit,
        over_budget=over_budget,
        compaction_triggered=compaction_decision.triggered or bool(trimmed_sections),
        trimmed_chars=max(
            0,
            unbudgeted_report.total_chars
            - (budgeted.total_chars + owner_persona_chars),
        ),
        trimmed_sections=trimmed_sections,
        compression_stage=_compression_stage(
            compression_reasons,
            trimmed_sections=trimmed_sections,
        ),
        compression_reasons=compression_reasons,
    )
    return AssistantContextPack(
        request=active_request,
        context_summary=context_summary,
        compactor_type=compactor_type,
        conversation_text=budgeted.conversation_text,
        memory_summaries=summaries,
        memory_text=budgeted.memory_text,
        memory_blocks=memory_blocks,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        plan_state=plan_state,
        observations=budgeted.observations,
        tool_specs=active_tool_specs,
        prompt_tool_specs=prompt_tool_specs,
        run_tool_catalog=tool_catalog.run_tool_catalog,
        tool_catalog_summary=tool_catalog.summary,
        tool_capabilities=tool_capabilities,
        skill_report=tool_capability_catalog.skill_report,
        context_sections=context_sections,
        context_source_report=build_context_source_report(
            state.context_source_result,
            context_sections,
        ),
        iteration=iteration,
        max_iterations=max_iterations,
        source_counts=source_counts,
        budget=final_budget,
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


def _context_summary(request: UserRequest) -> ContextSummary | None:
    summary = context_summary_from_metadata(request.metadata.get("context_summary"))
    if summary is not None:
        return summary
    return context_summary_from_metadata(request.metadata.get("session_context_summary"))


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


def _metadata_dict(request: UserRequest, key: str) -> dict[str, Any] | None:
    value = request.metadata.get(key)
    return dict(value) if isinstance(value, dict) else None


def _realtime_video_context(request: UserRequest) -> RealtimeVideoContext | None:
    if request.metadata.get("realtime_video_context_trusted") is not True:
        return None
    value = request.metadata.get("realtime_video_context")
    if not isinstance(value, dict):
        return None
    try:
        context = RealtimeVideoContext.model_validate(value)
    except (TypeError, ValueError):
        return None
    return None if context.status == "unavailable" else context


def _durable_task_context(request: UserRequest) -> tuple[dict[str, Any] | None, bool]:
    """Validate and whitelist the worker-owned snapshot before prompt exposure."""

    raw_snapshot = request.metadata.get("durable_task_snapshot")
    try:
        snapshot = DurableTaskSnapshot.model_validate(raw_snapshot)
    except (TypeError, ValueError):
        return None, False
    wait = snapshot.wait or {}
    payload: dict[str, Any] = {
        "task_id": snapshot.task_id,
        "objective": snapshot.objective,
        "active_constraints": list(snapshot.active_constraints),
        "task_status": snapshot.task_status,
        "plan_version": snapshot.plan_version,
        "plan": snapshot.plan.model_dump(mode="json"),
        "ready_step_ids": list(snapshot.ready_step_ids),
        "completed_steps": [
            {
                key: item[key]
                for key in ("step_id", "summary", "output_ref")
                if key in item
            }
            for item in snapshot.completed_steps
        ],
        "artifact_refs": [item.model_dump(mode="json") for item in snapshot.artifact_refs],
        "wait": (
            {
                key: wait[key]
                for key in ("kind", "step_id", "confirmation_id", "message", "missing_inputs")
                if key in wait
            }
            if wait
            else None
        ),
        "remaining_budget": dict(snapshot.remaining_budget),
    }
    clipped, trimmed = _clip_durable_prompt_value(payload)
    return clipped, trimmed


def _clip_durable_prompt_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        if len(value) <= 512:
            return value, False
        return value[:509] + "...", True
    if isinstance(value, list):
        items = value[:16]
        clipped_items = [_clip_durable_prompt_value(item) for item in items]
        return [item for item, _ in clipped_items], len(value) > 16 or any(flag for _, flag in clipped_items)
    if isinstance(value, dict):
        clipped_items = {
            key: _clip_durable_prompt_value(item)
            for key, item in value.items()
        }
        return (
            {key: item for key, (item, _) in clipped_items.items()},
            any(flag for _, flag in clipped_items.values()),
        )
    return value, False


def _source_counts(
    *,
    request: UserRequest,
    memory_summaries: list[str],
    memory_blocks: list[dict[str, Any]],
    realtime_task_state: dict[str, Any] | None,
    realtime_video_context: RealtimeVideoContext | None,
    durable_task_state: dict[str, Any] | None,
    observations: list[dict[str, Any]],
    tool_specs: list[ToolSpec],
    prompt_tool_specs: list[ToolSpec],
    tool_capabilities: list[ToolCapabilityDescriptor],
    context_sections: list[ContextSection],
    context_source_issue_count: int,
) -> dict[str, int]:
    conversation_history = request.metadata.get("conversation_history")
    artifact_refs = request.metadata.get("memory_context_refs")
    return {
        "conversation_turns": len(conversation_history) if isinstance(conversation_history, list) else 0,
        "conversation_recent_turns": _metadata_int(request, "conversation_context_recent_turns"),
        "conversation_compacted_turns": _metadata_int(request, "conversation_context_compacted_turns"),
        "memory_items": len(memory_summaries),
        "memory_blocks": len(memory_blocks),
        "realtime_task_state": 1 if realtime_task_state is not None else 0,
        "realtime_video_context": 1 if realtime_video_context is not None else 0,
        "durable_task_state": 1 if durable_task_state is not None else 0,
        "artifact_refs": len(artifact_refs) if isinstance(artifact_refs, list) else 0,
        "observations": len(observations),
        "tool_specs": len(tool_specs),
        "prompt_tool_specs": len(prompt_tool_specs),
        "tool_capabilities": len(tool_capabilities),
        "context_sections": len(context_sections),
        "context_source_issues": context_source_issue_count,
    }


def _budget_report(
    *,
    request: UserRequest,
    conversation_text: str,
    memory_text: str,
    realtime_task_state: dict[str, Any] | None,
    realtime_video_context: RealtimeVideoContext | None,
    durable_task_state: dict[str, Any] | None,
    plan_state: AssistantPlanContext,
    observations: list[dict[str, Any]],
    tool_specs: list[ToolSpec],
    tool_capabilities: list[ToolCapabilityDescriptor] | None = None,
    context_sections: list[ContextSection] | None = None,
    max_chars: int = 0,
    over_budget: bool = False,
    compaction_triggered: bool = False,
    trimmed_chars: int = 0,
    trimmed_sections: list[str] | None = None,
    compression_stage: str = COMPRESSION_STAGE_NONE,
    compression_reasons: list[str] | None = None,
) -> ContextBudgetReport:
    request_chars = len(request.text or "")
    conversation_chars = len(conversation_text)
    memory_chars = len(memory_text)
    realtime_task_state_chars = _json_chars(realtime_task_state) if realtime_task_state else 0
    realtime_video_context_chars = (
        _json_chars(realtime_video_context.model_dump(mode="json"))
        if realtime_video_context is not None
        else 0
    )
    durable_task_state_chars = _json_chars(durable_task_state) if durable_task_state else 0
    plan_chars = _json_chars(plan_state.model_dump(mode="json")) if _has_plan_context(plan_state) else 0
    observations_chars = _json_chars(observations)
    tool_spec_chars = _json_chars([prompt_tool_spec_payload(spec) for spec in tool_specs])
    tool_capability_chars = _json_chars(
        [descriptor.model_dump(mode="json") for descriptor in tool_capabilities or []]
    )
    owner_persona_chars = _owner_persona_chars(context_sections or [])
    total_chars = (
        request_chars
        + conversation_chars
        + memory_chars
        + realtime_task_state_chars
        + realtime_video_context_chars
        + durable_task_state_chars
        + plan_chars
        + observations_chars
        + tool_spec_chars
        + tool_capability_chars
        + owner_persona_chars
    )
    context_usage_ratio = total_chars / max_chars if max_chars > 0 else 0.0
    token_reporter = token_budget_reporter_from_request(request)
    token_budget = (
        token_reporter.report(
            request=request,
            sections={
                "request": request.text or "",
                "conversation": conversation_text,
                "memory": memory_text,
                "realtime_task_state": realtime_task_state or {},
                "realtime_video_context": (
                    realtime_video_context.model_dump(mode="json")
                    if realtime_video_context is not None
                    else {}
                ),
                "durable_task_state": durable_task_state or {},
                "plan": plan_state.model_dump(mode="json") if _has_plan_context(plan_state) else {},
                "observations": observations,
                "tool_spec": [spec.model_dump(mode="json") for spec in tool_specs],
                "tool_capability": [
                    descriptor.model_dump(mode="json") for descriptor in tool_capabilities or []
                ],
                "owner_persona": "\n\n".join(
                    section.content
                    for section in context_sections or []
                    if section.kind == "soul"
                ),
            },
        )
        if token_reporter is not None
        else None
    )
    return ContextBudgetReport(
        request_chars=request_chars,
        conversation_chars=conversation_chars,
        memory_chars=memory_chars,
        realtime_task_state_chars=realtime_task_state_chars,
        realtime_video_context_chars=realtime_video_context_chars,
        durable_task_state_chars=durable_task_state_chars,
        plan_chars=plan_chars,
        observations_chars=observations_chars,
        tool_spec_chars=tool_spec_chars,
        tool_capability_chars=tool_capability_chars,
        owner_persona_chars=owner_persona_chars,
        total_chars=total_chars,
        max_chars=max_chars,
        over_budget=over_budget,
        context_usage_ratio=context_usage_ratio,
        compaction_triggered=compaction_triggered,
        trimmed_chars=trimmed_chars,
        trimmed_sections=trimmed_sections or [],
        compression_stage=compression_stage,
        compression_reasons=compression_reasons or [],
        **(token_budget.model_dump(mode="json") if token_budget is not None else {}),
    )


def _compression_reasons(
    *,
    request: UserRequest,
    observations: list[dict[str, Any]],
    over_budget: bool,
    trimmed_sections: list[str],
    extra_reasons: list[str] | None = None,
) -> list[str]:
    reasons: list[str] = list(extra_reasons or [])
    if request.metadata.get("conversation_context_compacted") is True:
        reasons.append(COMPRESSION_REASON_CONVERSATION_COMPACTED)
    if any(observation.get("compacted") is True for observation in observations):
        reasons.append(COMPRESSION_REASON_OBSERVATION_COMPACTED)
    if over_budget:
        reasons.append(COMPRESSION_REASON_CONTEXT_OVER_BUDGET)
    if trimmed_sections:
        reasons.append(COMPRESSION_REASON_CONTEXT_BUDGET_TRIMMED)
    return _unique(reasons)


def _compression_stage(reasons: list[str], *, trimmed_sections: list[str]) -> str:
    if trimmed_sections:
        return COMPRESSION_STAGE_BUDGET_TRIMMED
    if reasons:
        return COMPRESSION_STAGE_COMPACTED
    return COMPRESSION_STAGE_NONE


def _has_plan_context(plan_state: AssistantPlanContext) -> bool:
    return plan_state.current_plan is not None or plan_state.plan_status != "none"


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _effective_context_budget_limit(
    *,
    request: UserRequest,
    base_max_chars: int,
    tool_specs: list[ToolSpec],
    tool_capabilities: list[ToolCapabilityDescriptor],
) -> int:
    """Add fixed schema headroom unless the caller supplied a hard total limit."""

    if CONTEXT_BUDGET_METADATA_KEY in request.metadata:
        return base_max_chars
    tool_schema_chars = _json_chars(
        [prompt_tool_spec_payload(spec) for spec in tool_specs]
    )
    capability_schema_chars = _json_chars(
        [descriptor.model_dump(mode="json") for descriptor in tool_capabilities]
    )
    return base_max_chars + tool_schema_chars + capability_schema_chars


def _metadata_int(request: UserRequest, key: str) -> int:
    value = request.metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def _owner_persona_chars(sections: list[ContextSection]) -> int:
    return sum(len(section.content) for section in sections if section.kind == "soul")


def _fit_context_sections_to_budget(
    sections: list[ContextSection],
    *,
    available_chars: int,
) -> tuple[list[ContextSection], list[str]]:
    """Fit validated sections at whole-paragraph boundaries."""

    remaining = max(0, available_chars)
    fitted: list[ContextSection] = []
    trimmed: list[str] = []
    for section in sorted(sections, key=lambda item: item.priority):
        if len(section.content) <= remaining:
            fitted.append(section)
            remaining -= len(section.content)
            continue
        content = _fit_section_content(section.content, max_chars=remaining)
        if content:
            notes = list(section.notes)
            if "budget_trimmed" not in notes:
                notes.append("budget_trimmed")
            fitted.append(section.model_copy(update={"content": content, "notes": notes}))
            remaining -= len(content)
        if section.kind == "soul" and "owner_persona" not in trimmed:
            trimmed.append("owner_persona")
    return fitted, trimmed


def _fit_section_content(value: str, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    selected: list[str] = []
    for block in value.split("\n\n"):
        candidate = "\n\n".join([*selected, block])
        if len(candidate) > max_chars:
            break
        selected.append(block)
    return "\n\n".join(selected)


class _BudgetedContext:
    def __init__(
        self,
        *,
        conversation_text: str,
        memory_text: str,
        observations: list[dict[str, Any]],
        total_chars: int,
        trimmed_sections: list[str],
    ) -> None:
        self.conversation_text = conversation_text
        self.memory_text = memory_text
        self.observations = observations
        self.total_chars = total_chars
        self.trimmed_sections = trimmed_sections


def _enforce_context_budget(
    *,
    request: UserRequest,
    conversation_text: str,
    memory_text: str,
    realtime_task_state: dict[str, Any] | None,
    realtime_video_context: RealtimeVideoContext | None,
    durable_task_state: dict[str, Any] | None,
    observations: list[dict[str, Any]],
    plan_state: AssistantPlanContext,
    tool_specs: list[ToolSpec],
    tool_capabilities: list[ToolCapabilityDescriptor],
    max_chars: int,
) -> _BudgetedContext:
    budget = _budget_report(
        request=request,
        conversation_text=conversation_text,
        memory_text=memory_text,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        plan_state=plan_state,
        observations=observations,
        tool_specs=tool_specs,
        tool_capabilities=tool_capabilities,
    )
    if budget.total_chars <= max_chars:
        return _BudgetedContext(
            conversation_text=conversation_text,
            memory_text=memory_text,
            observations=observations,
            total_chars=budget.total_chars,
            trimmed_sections=[],
        )

    trimmed_sections: list[str] = []
    fixed_chars = (
        budget.request_chars
        + budget.realtime_task_state_chars
        + budget.realtime_video_context_chars
        + budget.durable_task_state_chars
        + budget.plan_chars
        + budget.tool_spec_chars
        + budget.tool_capability_chars
    )
    available = max(0, max_chars - fixed_chars)

    budgeted_memory_text = memory_text
    observation_chars = _json_chars(observations)
    target_memory_chars = max(0, available - observation_chars - len(conversation_text))
    if len(budgeted_memory_text) > target_memory_chars:
        budgeted_memory_text = _clip_text_to_chars(budgeted_memory_text, target_memory_chars)
        trimmed_sections.append("memory")

    budgeted_conversation_text = conversation_text
    used_after_memory = observation_chars + len(budgeted_memory_text)
    target_conversation_chars = max(0, available - used_after_memory)
    if len(budgeted_conversation_text) > target_conversation_chars:
        budgeted_conversation_text = _clip_text_to_chars(
            budgeted_conversation_text,
            target_conversation_chars,
        )
        trimmed_sections.append("conversation")

    budgeted_observations = observations
    used_after_text_context = len(budgeted_memory_text) + len(budgeted_conversation_text)
    target_observation_chars = max(0, available - used_after_text_context)
    if _json_chars(budgeted_observations) > target_observation_chars:
        budgeted_observations = _trim_observations_to_chars(
            budgeted_observations,
            max_chars=target_observation_chars,
        )
        trimmed_sections.append("observations")

    final_budget = _budget_report(
        request=request,
        conversation_text=budgeted_conversation_text,
        memory_text=budgeted_memory_text,
        realtime_task_state=realtime_task_state,
        realtime_video_context=realtime_video_context,
        durable_task_state=durable_task_state,
        plan_state=plan_state,
        observations=budgeted_observations,
        tool_specs=tool_specs,
        tool_capabilities=tool_capabilities,
    )
    return _BudgetedContext(
        conversation_text=budgeted_conversation_text,
        memory_text=budgeted_memory_text,
        observations=budgeted_observations,
        total_chars=final_budget.total_chars,
        trimmed_sections=trimmed_sections,
    )


def _trim_observations_to_chars(
    observations: list[dict[str, Any]],
    *,
    max_chars: int,
) -> list[dict[str, Any]]:
    if max_chars <= 0 or not observations:
        return []
    if _json_chars(observations) <= max_chars:
        return observations

    candidate = [_summarize_observation_for_budget(observation) for observation in observations]
    while len(candidate) > 1 and _json_chars(candidate) > max_chars:
        candidate = candidate[1:]
    if _json_chars(candidate) <= max_chars:
        return candidate

    latest = candidate[-1]
    latest_summary = _clip_text_to_chars(str(latest.get("summary") or ""), 80)
    fallback = [
        {
            "tool_name": latest.get("tool_name"),
            "status": latest.get("status"),
            "summary": latest_summary,
            "budget_trimmed": True,
        }
    ]
    return fallback if _json_chars(fallback) <= max_chars else []


def _summarize_observation_for_budget(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": observation.get("tool_name"),
        "status": observation.get("status"),
        "summary": _clip_text_to_chars(str(observation.get("summary") or ""), 160),
        "output_ref": observation.get("output_ref"),
        "error_code": observation.get("error_code"),
        "error_message": _clip_text_to_chars(str(observation.get("error_message") or ""), 160),
        "budget_trimmed": True,
    }


def _clip_text_to_chars(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars <= 20:
        return value[:max_chars]
    return value[: max_chars - 15].rstrip() + "...[trimmed]"


@dataclass(frozen=True)
class _ConversationTurnForSummary:
    user_text: str
    assistant_text: str
    run_id: str
    trace_id: str


def _conversation_history_turns(request: UserRequest) -> list[_ConversationTurnForSummary]:
    history = request.metadata.get("conversation_history")
    if not isinstance(history, list):
        return []
    turns: list[_ConversationTurnForSummary] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        turns.append(
            _ConversationTurnForSummary(
                user_text=str(item.get("user_text") or ""),
                assistant_text=str(item.get("assistant_text") or ""),
                run_id=str(item.get("run_id") or ""),
                trace_id=str(item.get("trace_id") or ""),
            )
        )
    return turns


def _conversation_turns_to_compact(
    request: UserRequest,
    *,
    context_policy: ContextPolicy,
) -> list[_ConversationTurnForSummary]:
    turns = _conversation_history_turns(request)
    if not turns:
        return []
    selection = select_conversation_window(
        turns,
        recent_turns=context_policy.keep_recent_turns,
        metadata=request.metadata,
        context_policy=context_policy,
        force_minimum_recent=_force_minimum_recent_window(request),
    )
    return list(selection.compacted_turns)


def _force_minimum_recent_window(request: UserRequest) -> bool:
    metadata = request.metadata
    if metadata.get("compact_context") is True:
        return True
    for key in ("slash_command", "command"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip() == "/compact":
            return True
    return bool((request.text or "").strip() == "/compact")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
