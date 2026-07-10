"""Unified read-only policy view for governed tool calls."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.tools import (
    ToolSideEffectLevel,
    ToolSideEffectPolicy,
    ToolSpec,
)
from assistant_agent.services.tool_risk_gate import (
    ToolRiskGateLevel,
    risk_gate_level_for_policy,
    tool_owns_confirmation,
)
from assistant_agent.tools.registry import tool_side_effect_policy


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
    idempotency_required: bool = False
    description: str = ""
    compensation_hint: str | None = None


class ToolPolicyInterpreter:
    """Interpret existing ToolSpec side-effect policy without changing behavior."""

    def view_for_spec(self, spec: ToolSpec) -> ToolPolicyView:
        """Return the current policy view for an explicit tool spec."""

        return self.view_for_policy(tool_name=spec.name, policy=spec.side_effect)

    def view_for_tool_name(self, tool_name: str) -> ToolPolicyView:
        """Return the current policy view for a tool name using registry defaults."""

        return self.view_for_policy(
            tool_name=tool_name,
            policy=tool_side_effect_policy(tool_name),
        )

    def view_for_policy(
        self,
        *,
        tool_name: str,
        policy: ToolSideEffectPolicy,
    ) -> ToolPolicyView:
        """Return the current policy view for a policy payload."""

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
            idempotency_required=risk_gate_level == "soft_gate",
            description=policy.description,
            compensation_hint=policy.compensation_hint,
        )


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
