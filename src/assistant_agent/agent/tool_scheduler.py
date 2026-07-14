"""Conservative scheduling for provider-native tool batches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.agent.action_validator import ActionValidationResult
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.tools import RealtimeToolSafety, ToolDependencyMode, ToolSpec
from assistant_agent.services.tool_policy import ToolPolicyInterpreter


ToolScheduleMode = Literal["parallel", "serial"]

READ_ONLY_SIDE_EFFECT_LEVELS = {"none", "local_read", "external_read"}


@dataclass(frozen=True)
class ScheduledToolCall:
    """One provider-native call after internal decision normalization and validation."""

    call_index: int
    decision: AssistantDecision
    validation: ActionValidationResult
    side_effect_level: str
    requires_confirmation: bool
    dependency_mode: ToolDependencyMode = "requires_prior_observation"
    concurrency_group: str | None = None
    resource_reads: tuple[str, ...] = ()
    resource_writes: tuple[str, ...] = ()
    realtime_safety: RealtimeToolSafety = "needs_confirmation"
    validation_latency_ms: int = 0
    native_call_id: str | None = None
    tool_spec: ToolSpec | None = None

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
            "dependency_mode": self.dependency_mode,
            "concurrency_group": self.concurrency_group,
            "resource_reads": list(self.resource_reads),
            "resource_writes": list(self.resource_writes),
            "realtime_safety": self.realtime_safety,
            "validation_latency_ms": self.validation_latency_ms,
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
            "dependency_modes": [call.dependency_mode for call in self.calls],
            "realtime_safety": [call.realtime_safety for call in self.calls],
            "concurrency_groups": [call.concurrency_group for call in self.calls],
            "resource_reads": [list(call.resource_reads) for call in self.calls],
            "resource_writes": [list(call.resource_writes) for call in self.calls],
        }


@dataclass(frozen=True)
class ToolSchedule:
    """A conservative schedule for one provider-native tool batch."""

    groups: tuple[ToolExecutionGroup, ...]
    reason: str
    rejected_call: ScheduledToolCall | None = None

    def to_metadata(self) -> dict[str, Any]:
        parallel_tool_count = sum(len(group.calls) for group in self.groups if group.mode == "parallel")
        calls = [call for group in self.groups for call in group.calls]
        return {
            "schema_version": "native_tool_schedule_v1",
            "reason": self.reason,
            "group_count": len(self.groups),
            "parallel_tool_count": parallel_tool_count,
            "dependency_modes": [call.dependency_mode for call in calls],
            "realtime_safety": [call.realtime_safety for call in calls],
            "concurrency_groups": [call.concurrency_group for call in calls],
            "groups": [group.to_metadata() for group in self.groups],
            "rejected_call": self.rejected_call.to_metadata() if self.rejected_call is not None else None,
        }


def build_scheduled_tool_call(
    *,
    call_index: int,
    decision: AssistantDecision,
    validation: ActionValidationResult,
    validation_latency_ms: int = 0,
    native_call_id: str | None = None,
    tool_spec: ToolSpec | None = None,
) -> ScheduledToolCall:
    """Attach static safety metadata to a normalized and validated call."""

    interpreter = ToolPolicyInterpreter()
    policy = (
        interpreter.view_for_spec(tool_spec)
        if tool_spec is not None
        else interpreter.view_for_tool_name(decision.tool_name or "")
    )
    return ScheduledToolCall(
        call_index=call_index,
        decision=decision,
        validation=validation,
        side_effect_level=policy.side_effect_level,
        requires_confirmation=policy.requires_confirmation,
        dependency_mode=policy.dependency_mode,
        concurrency_group=policy.concurrency_group,
        resource_reads=tuple(policy.resource_reads),
        resource_writes=tuple(policy.resource_writes),
        realtime_safety=policy.realtime_safety,
        validation_latency_ms=max(0, validation_latency_ms),
        native_call_id=native_call_id,
        tool_spec=tool_spec,
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

    if any(call.dependency_mode == "terminal" for call in calls):
        return _serial_schedule(calls, "terminal_tool")

    if any(call.dependency_mode == "requires_prior_observation" for call in calls):
        return _serial_schedule(calls, "requires_prior_observation")

    if any(call.realtime_safety == "unsafe" for call in calls):
        return _serial_schedule(calls, "realtime_unsafe")

    if any(call.realtime_safety == "needs_confirmation" for call in calls):
        return _serial_schedule(calls, "realtime_confirmation_required")

    if any(not _is_read_only(call) for call in calls):
        return _serial_schedule(calls, "non_read_only_tool")

    if any(call.requires_confirmation for call in calls):
        return _serial_schedule(calls, "confirmation_required")

    tool_names = [call.tool_name for call in calls]
    if len(set(tool_names)) != len(tool_names):
        return _serial_schedule(calls, "duplicate_tool_name")

    if any(call.resource_writes for call in calls):
        return _serial_schedule(calls, "resource_write_conflict")

    if _has_concurrency_group_conflict(calls):
        return _serial_schedule(calls, "concurrency_group_conflict")

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


def _has_concurrency_group_conflict(calls: list[ScheduledToolCall]) -> bool:
    groups = [call.concurrency_group for call in calls if call.concurrency_group]
    return len(groups) != len(set(groups))
