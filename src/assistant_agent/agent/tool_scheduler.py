"""Conservative scheduling for provider-native tool batches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.agent.action_validator import ActionValidationResult
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.services.tool_policy import ToolPolicyInterpreter


ToolScheduleMode = Literal["parallel", "serial"]

READ_ONLY_SIDE_EFFECT_LEVELS = {"none", "local_read", "external_read"}
DEPENDENT_TOOL_ORDER_PAIRS = {
    ("product_search", "price_compare"),
}


@dataclass(frozen=True)
class ScheduledToolCall:
    """One provider-native call after internal decision normalization and validation."""

    call_index: int
    decision: AssistantDecision
    validation: ActionValidationResult
    side_effect_level: str
    requires_confirmation: bool
    native_call_id: str | None = None

    @property
    def tool_name(self) -> str:
        return self.decision.tool_name or "unknown"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "call_index": self.call_index,
            "tool_name": self.tool_name,
            "native_call_id": self.native_call_id,
            "validation": {
                "accepted": self.validation.accepted,
                "code": self.validation.code,
            },
            "side_effect_level": self.side_effect_level,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True)
class ToolExecutionGroup:
    """A group of calls that share an execution mode."""

    mode: ToolScheduleMode
    reason: str
    calls: tuple[ScheduledToolCall, ...]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "call_indices": [call.call_index for call in self.calls],
            "tool_names": [call.tool_name for call in self.calls],
            "side_effect_levels": [call.side_effect_level for call in self.calls],
        }


@dataclass(frozen=True)
class ToolSchedule:
    """A conservative schedule for one provider-native tool batch."""

    groups: tuple[ToolExecutionGroup, ...]
    reason: str
    rejected_call: ScheduledToolCall | None = None

    def to_metadata(self) -> dict[str, Any]:
        parallel_tool_count = sum(len(group.calls) for group in self.groups if group.mode == "parallel")
        return {
            "schema_version": "native_tool_schedule_v1",
            "reason": self.reason,
            "group_count": len(self.groups),
            "parallel_tool_count": parallel_tool_count,
            "groups": [group.to_metadata() for group in self.groups],
            "rejected_call": self.rejected_call.to_metadata() if self.rejected_call is not None else None,
        }


def build_scheduled_tool_call(
    *,
    call_index: int,
    decision: AssistantDecision,
    validation: ActionValidationResult,
    native_call_id: str | None = None,
) -> ScheduledToolCall:
    """Attach static safety metadata to a normalized and validated call."""

    policy = ToolPolicyInterpreter().view_for_tool_name(decision.tool_name or "")
    return ScheduledToolCall(
        call_index=call_index,
        decision=decision,
        validation=validation,
        side_effect_level=policy.side_effect_level,
        requires_confirmation=policy.requires_confirmation,
        native_call_id=native_call_id,
    )


def plan_tool_schedule(
    calls: list[ScheduledToolCall],
    *,
    remaining_tool_budget: int,
    max_parallel_tools: int = 4,
    provider_budget_parallel_safe: bool = True,
) -> ToolSchedule:
    """Plan a small, safe schedule for a single provider-native tool batch."""

    if not calls:
        return ToolSchedule(groups=(), reason="empty_batch")

    rejected_call = next((call for call in calls if not call.validation.accepted), None)
    if rejected_call is not None:
        return _serial_schedule(calls, "validation_rejected", rejected_call=rejected_call)

    if len(calls) == 1:
        return _serial_schedule(calls, "single_tool")

    if remaining_tool_budget < len(calls):
        return _serial_schedule(calls, "tool_iteration_budget_limited")

    if len(calls) > max_parallel_tools:
        return _serial_schedule(calls, "parallel_batch_too_large")

    if not provider_budget_parallel_safe:
        return _serial_schedule(calls, "provider_budget_requires_serial_check")

    if any(not _is_read_only(call) for call in calls):
        return _serial_schedule(calls, "non_read_only_tool")

    if any(call.requires_confirmation for call in calls):
        return _serial_schedule(calls, "confirmation_required")

    tool_names = [call.tool_name for call in calls]
    if len(set(tool_names)) != len(tool_names):
        return _serial_schedule(calls, "duplicate_tool_name")

    if _has_dependent_tool_order(tool_names):
        return _serial_schedule(calls, "dependent_tool_order")

    group = ToolExecutionGroup(mode="parallel", reason="read_only_independent", calls=tuple(calls))
    return ToolSchedule(groups=(group,), reason="read_only_independent")


def _serial_schedule(
    calls: list[ScheduledToolCall],
    reason: str,
    *,
    rejected_call: ScheduledToolCall | None = None,
) -> ToolSchedule:
    group = ToolExecutionGroup(mode="serial", reason=reason, calls=tuple(calls))
    return ToolSchedule(groups=(group,), reason=reason, rejected_call=rejected_call)


def _is_read_only(call: ScheduledToolCall) -> bool:
    return call.side_effect_level in READ_ONLY_SIDE_EFFECT_LEVELS


def _has_dependent_tool_order(tool_names: list[str]) -> bool:
    for earlier, later in DEPENDENT_TOOL_ORDER_PAIRS:
        if earlier in tool_names and later in tool_names and tool_names.index(earlier) < tool_names.index(later):
            return True
    return False
