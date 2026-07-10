"""Unified read-only policy view for governed tool calls."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import (
    RealtimeToolSafety,
    ToolDependencyMode,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolRisk,
    ToolSideEffectLevel,
    ToolSideEffectPolicy,
    ToolSpec,
)
from assistant_agent.services.tool_risk_gate import (
    ToolRiskGateLevel,
    risk_gate_level_for_policy,
    tool_owns_confirmation,
)
from assistant_agent.tools.registry import tool_execution_policy, tool_side_effect_policy


TOOL_POLICY_VIEW_SCHEMA_VERSION = "tool_policy_view_v1"
ToolConfirmationOwner = Literal["none", "tool", "runtime"]


class ToolPolicyView(BaseModel):
    """Prompt-safe, read-only interpretation of one tool's current policy."""

    schema_version: str = TOOL_POLICY_VIEW_SCHEMA_VERSION
    tool_name: str = Field(min_length=1)
    side_effect_level: ToolSideEffectLevel
    risk_gate_level: ToolRiskGateLevel
    requires_confirmation: bool = False
    confirmation_kind: str | None = None
    confirmation_owner: ToolConfirmationOwner = "none"
    tool_owned_confirmation: bool = False
    auto_executable: bool = False
    dependency_mode: ToolDependencyMode = "requires_prior_observation"
    concurrency_group: str | None = None
    resource_reads: list[str] = Field(default_factory=list)
    resource_writes: list[str] = Field(default_factory=list)
    realtime_safety: RealtimeToolSafety = "needs_confirmation"
    idempotency_required: bool = False
    description: str = ""
    compensation_hint: str | None = None
    risk: ToolRisk | None = None
    realtime_mode: str | None = None
    approval_mode: str | None = None
    timeout_s: int | None = None
    retry_count: int | None = None
    idempotency: str | None = None
    max_result_chars: int | None = None
    reads_private_data: bool = False
    writes_private_data: bool = False
    sends_data_external: bool = False
    redact_in_trace: bool = False
    toolset: str | None = None
    tags: list[str] = Field(default_factory=list)
    requires_env: list[str] = Field(default_factory=list)
    enabled_by_default: bool = True
    skill_only: bool = False


class ToolPolicyInterpreter:
    """Interpret existing ToolSpec side-effect policy without changing behavior."""

    def view_for_spec(self, spec: ToolSpec) -> ToolPolicyView:
        """Return the current policy view for an explicit tool spec."""

        if spec.policy is not None:
            return self.view_for_metadata(
                tool_name=spec.name,
                metadata=spec.policy,
                execution=spec.execution,
            )
        return self.view_for_policy(
            tool_name=spec.name,
            policy=spec.side_effect,
            execution=spec.execution,
        )

    def view_for_metadata(
        self,
        *,
        tool_name: str,
        metadata: ToolPolicyMetadata,
        execution: ToolExecutionPolicy | None = None,
    ) -> ToolPolicyView:
        """Return the current policy view for explicit governance metadata."""

        execution_policy = execution or ToolExecutionPolicy()
        side_effect_policy = _side_effect_policy_from_metadata(metadata)
        risk_gate_level = risk_gate_level_for_policy(side_effect_policy)
        idempotency_required = (
            metadata.execution.idempotency == "required" or risk_gate_level == "soft_gate"
        )
        tool_owned_confirmation = side_effect_policy.requires_confirmation and tool_owns_confirmation(
            tool_name
        )
        return ToolPolicyView(
            tool_name=tool_name,
            side_effect_level=side_effect_policy.level,
            risk_gate_level=risk_gate_level,
            requires_confirmation=side_effect_policy.requires_confirmation,
            confirmation_kind=side_effect_policy.confirmation_kind,
            confirmation_owner=_confirmation_owner(
                requires_confirmation=side_effect_policy.requires_confirmation,
                tool_owned_confirmation=tool_owned_confirmation,
            ),
            tool_owned_confirmation=tool_owned_confirmation,
            auto_executable=risk_gate_level == "auto",
            dependency_mode=execution_policy.dependency_mode,
            concurrency_group=execution_policy.concurrency_group,
            resource_reads=list(execution_policy.resource_reads),
            resource_writes=list(execution_policy.resource_writes),
            realtime_safety=execution_policy.realtime_safety,
            idempotency_required=idempotency_required,
            description=side_effect_policy.description,
            compensation_hint=side_effect_policy.compensation_hint,
            risk=metadata.risk,
            realtime_mode=metadata.realtime.mode,
            approval_mode=metadata.approval.mode,
            timeout_s=metadata.execution.timeout_s,
            retry_count=metadata.execution.retry_count,
            idempotency=metadata.execution.idempotency,
            max_result_chars=metadata.execution.max_result_chars,
            reads_private_data=metadata.data.reads_private_data,
            writes_private_data=metadata.data.writes_private_data,
            sends_data_external=metadata.data.sends_data_external,
            redact_in_trace=metadata.data.redact_in_trace,
            toolset=metadata.visibility.toolset,
            tags=list(metadata.visibility.tags),
            requires_env=list(metadata.visibility.requires_env),
            enabled_by_default=metadata.visibility.enabled_by_default,
            skill_only=metadata.visibility.skill_only,
        )

    def view_for_tool_name(self, tool_name: str) -> ToolPolicyView:
        """Return the current policy view for a tool name using registry defaults."""

        return self.view_for_policy(
            tool_name=tool_name,
            policy=tool_side_effect_policy(tool_name),
            execution=tool_execution_policy(tool_name),
        )

    def view_for_policy(
        self,
        *,
        tool_name: str,
        policy: ToolSideEffectPolicy,
        execution: ToolExecutionPolicy | None = None,
    ) -> ToolPolicyView:
        """Return the current policy view for a policy payload."""

        execution_policy = execution or ToolExecutionPolicy()
        risk_gate_level = risk_gate_level_for_policy(policy)
        tool_owned_confirmation = policy.requires_confirmation and tool_owns_confirmation(
            tool_name
        )
        return ToolPolicyView(
            tool_name=tool_name,
            side_effect_level=policy.level,
            risk_gate_level=risk_gate_level,
            requires_confirmation=policy.requires_confirmation,
            confirmation_kind=policy.confirmation_kind,
            confirmation_owner=_confirmation_owner(
                requires_confirmation=policy.requires_confirmation,
                tool_owned_confirmation=tool_owned_confirmation,
            ),
            tool_owned_confirmation=tool_owned_confirmation,
            auto_executable=risk_gate_level == "auto",
            dependency_mode=execution_policy.dependency_mode,
            concurrency_group=execution_policy.concurrency_group,
            resource_reads=list(execution_policy.resource_reads),
            resource_writes=list(execution_policy.resource_writes),
            realtime_safety=execution_policy.realtime_safety,
            idempotency_required=risk_gate_level == "soft_gate",
            description=policy.description,
            compensation_hint=policy.compensation_hint,
        )


def _side_effect_policy_from_metadata(metadata: ToolPolicyMetadata) -> ToolSideEffectPolicy:
    side_effect_level = _side_effect_level_for_risk(metadata.risk)
    requires_confirmation = _requires_confirmation(metadata)
    return ToolSideEffectPolicy(
        level=side_effect_level,
        requires_confirmation=requires_confirmation,
        description=f"Declared tool policy risk={metadata.risk}.",
        confirmation_kind=metadata.approval.confirmation_kind,
    )


def _side_effect_level_for_risk(risk: ToolRisk) -> ToolSideEffectLevel:
    if risk == "pure":
        return "none"
    if risk in {"local_read", "external_read"}:
        return risk
    if risk == "transactional":
        return "compensatable"
    return "committed"


def _requires_confirmation(metadata: ToolPolicyMetadata) -> bool:
    if metadata.approval.mode == "never":
        return False
    if metadata.approval.mode == "always":
        return True
    return metadata.risk in {
        "local_write",
        "external_write",
        "transactional",
        "destructive",
    }


def _confirmation_owner(
    *,
    requires_confirmation: bool,
    tool_owned_confirmation: bool,
) -> ToolConfirmationOwner:
    if not requires_confirmation:
        return "none"
    if tool_owned_confirmation:
        return "tool"
    return "runtime"
