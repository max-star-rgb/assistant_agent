"""Runtime risk gate and idempotency ledger for tool execution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.tool_policy import (
    ToolPolicyInterpreter,
    ToolPolicyView,
    ToolRiskGateLevel,
    risk_gate_level_for_policy,
    tool_owns_confirmation,
)


TOOL_RISK_GATE_SCHEMA_VERSION = "tool_risk_gate_v1"
ToolIdempotencyStatus = Literal["committed", "pending_confirmation"]



class ToolRiskDecision(BaseModel):
    """Prompt-safe execution decision derived from static side-effect policy."""

    schema_version: str = TOOL_RISK_GATE_SCHEMA_VERSION
    tool_name: str = Field(min_length=1)
    level: ToolRiskGateLevel
    side_effect_level: str
    enabled: bool = True
    allow_execute: bool = True
    requires_confirmation: bool = False
    confirmation_kind: str | None = None
    reason: str = ""
    idempotency_required: bool = False
    idempotency_key: str | None = None
    idempotency_generated: bool = False

    def risk_summary(self) -> dict[str, Any]:
        return _drop_none(
            {
                "schema_version": self.schema_version,
                "level": self.level,
                "side_effect_level": self.side_effect_level,
                "enabled": self.enabled,
                "allow_execute": self.allow_execute,
                "requires_confirmation": self.requires_confirmation,
                "confirmation_kind": self.confirmation_kind,
                "reason": self.reason,
            }
        )

    def idempotency_summary(self, *, duplicate_suppressed: bool = False) -> dict[str, Any]:
        return _drop_none(
            {
                "key": self.idempotency_key,
                "present": self.idempotency_key is not None,
                "required": self.idempotency_required,
                "generated": self.idempotency_generated,
                "duplicate_suppressed": duplicate_suppressed,
            }
        )


class ToolIdempotencyRecord(BaseModel):
    """Prompt-safe record for one committed side-effecting tool execution."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: ToolIdempotencyStatus
    side_effect_level: str
    output_ref: str | None = None
    summary: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolIdempotencyLedger(Protocol):
    """Storage boundary for tool idempotency records."""

    def get(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        idempotency_key: str,
    ) -> ToolIdempotencyRecord | None:
        """Return an existing idempotency record when present."""

    def record(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        idempotency_key: str,
        status: ToolIdempotencyStatus,
        side_effect_level: str,
        output_ref: str | None = None,
        summary: str = "",
    ) -> ToolIdempotencyRecord:
        """Store a prompt-safe idempotency record."""


class InMemoryToolIdempotencyLedger:
    """Process-local idempotency ledger keyed by user/session/tool/key."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str], ToolIdempotencyRecord] = {}

    @property
    def record_count(self) -> int:
        return len(self._records)

    def get(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        idempotency_key: str,
    ) -> ToolIdempotencyRecord | None:
        record = self._records.get((user_id, session_id, tool_name, idempotency_key))
        return record.model_copy(deep=True) if record is not None else None

    def record(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        idempotency_key: str,
        status: ToolIdempotencyStatus,
        side_effect_level: str,
        output_ref: str | None = None,
        summary: str = "",
    ) -> ToolIdempotencyRecord:
        record = ToolIdempotencyRecord(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            status=status,
            side_effect_level=side_effect_level,
            output_ref=output_ref,
            summary=_clip(summary, max_chars=240),
        )
        self._records[(user_id, session_id, tool_name, idempotency_key)] = record
        return record.model_copy(deep=True)


_DEFAULT_IDEMPOTENCY_LEDGER = InMemoryToolIdempotencyLedger()


def get_default_tool_idempotency_ledger() -> ToolIdempotencyLedger:
    """Return the process-local tool idempotency ledger."""

    return _DEFAULT_IDEMPOTENCY_LEDGER


def evaluate_tool_risk(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    request: UserRequest,
    state: AgentState,
    step_id: str | None,
    policy_view: ToolPolicyView | None = None,
) -> ToolRiskDecision:
    """Return the runtime risk/idempotency decision for a tool call."""

    view = policy_view or ToolPolicyInterpreter().view_for_tool_name(tool_name)
    level = view.risk_gate_level
    enabled = True
    supplied_key = _metadata_string(tool_input.get("idempotency_key"))

    if level == "auto":
        return ToolRiskDecision(
            tool_name=tool_name,
            level=level,
            side_effect_level=view.side_effect_level,
            enabled=enabled,
            allow_execute=True,
            requires_confirmation=False,
            reason="read_only_or_local_safe",
        )

    if level == "soft_gate":
        key = supplied_key
        generated = False
        if key is None and _can_generate_idempotency_key(step_id):
            key = _generated_idempotency_key(tool_name=tool_name, request=request, state=state, step_id=step_id)
            generated = True
        return ToolRiskDecision(
            tool_name=tool_name,
            level=level,
            side_effect_level=view.side_effect_level,
            enabled=enabled,
            allow_execute=True,
            requires_confirmation=False,
            reason="compensatable_side_effect",
            idempotency_required=True,
            idempotency_key=key,
            idempotency_generated=generated,
        )

    requires_idempotency = view.idempotency_required
    if not view.requires_confirmation:
        if requires_idempotency and supplied_key is None:
            return ToolRiskDecision(
                tool_name=tool_name,
                level=level,
                side_effect_level=view.side_effect_level,
                enabled=enabled,
                allow_execute=False,
                requires_confirmation=False,
                reason="idempotency_key_required",
                idempotency_required=True,
            )
        return ToolRiskDecision(
            tool_name=tool_name,
            level=level,
            side_effect_level=view.side_effect_level,
            enabled=enabled,
            allow_execute=True,
            requires_confirmation=False,
            reason="policy_allows_without_confirmation",
            idempotency_required=requires_idempotency,
            idempotency_key=supplied_key,
        )

    confirmation_owned_by_tool = view.confirmation_owner == "tool"
    confirmed = _tool_confirmation_granted(request, tool_name)
    if confirmed:
        if requires_idempotency and supplied_key is None:
            return ToolRiskDecision(
                tool_name=tool_name,
                level=level,
                side_effect_level=view.side_effect_level,
                enabled=enabled,
                allow_execute=False,
                requires_confirmation=True,
                confirmation_kind=view.confirmation_kind or "tool_execution",
                reason="idempotency_key_required_after_confirmation",
                idempotency_required=True,
            )
        return ToolRiskDecision(
            tool_name=tool_name,
            level=level,
            side_effect_level=view.side_effect_level,
            enabled=enabled,
            allow_execute=True,
            requires_confirmation=False,
            confirmation_kind=view.confirmation_kind,
            reason="user_confirmation_granted",
            idempotency_required=requires_idempotency,
            idempotency_key=supplied_key,
        )
    allow_execute = confirmation_owned_by_tool
    return ToolRiskDecision(
        tool_name=tool_name,
        level=level,
        side_effect_level=view.side_effect_level,
        enabled=enabled,
        allow_execute=allow_execute,
        requires_confirmation=True,
        confirmation_kind=view.confirmation_kind or "tool_execution",
        reason="tool_owned_confirmation" if confirmation_owned_by_tool else "confirmation_required",
        idempotency_required=requires_idempotency,
        idempotency_key=supplied_key,
    )


def duplicate_suppressed_result(
    *,
    tool_name: str,
    record: ToolIdempotencyRecord,
    decision: ToolRiskDecision,
    latency_ms: int,
) -> ToolResult:
    """Build a safe duplicate-suppressed result without replaying raw output."""

    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={
            "status": "duplicate_suppressed",
            "summary": record.summary or "Previous tool execution already completed.",
            "side_effect_level": record.side_effect_level,
            "idempotency": {
                **decision.idempotency_summary(duplicate_suppressed=True),
                "status": record.status,
            },
        },
        output_ref=record.output_ref,
        latency_ms=latency_ms,
    )


def confirmation_required_result(
    *,
    tool_name: str,
    decision: ToolRiskDecision,
    latency_ms: int,
) -> ToolResult:
    """Build a pending-confirmation result without executing the tool."""

    idempotency_only = decision.reason == "idempotency_key_required"
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={
            "status": "idempotency_key_required" if idempotency_only else "confirmation_required",
            "summary": (
                "Tool execution requires an idempotency key before continuing."
                if idempotency_only
                else "Tool execution requires user confirmation before continuing."
            ),
            "requires_confirmation": decision.requires_confirmation,
            "requires_idempotency_key": idempotency_only,
            "confirmation_kind": decision.confirmation_kind,
            "side_effect_level": "pending_confirmation",
            "risk_gate": decision.risk_summary(),
        },
        output_ref=f"local://tool-confirmations/{tool_name}",
        latency_ms=latency_ms,
    )


def record_successful_idempotent_result(
    *,
    ledger: ToolIdempotencyLedger,
    decision: ToolRiskDecision,
    state: AgentState,
    result: ToolResult,
) -> ToolIdempotencyRecord | None:
    """Record a committed side-effecting result in the idempotency ledger."""

    if not should_record_idempotent_result(decision, result):
        return None
    return ledger.record(
        user_id=state.user_id,
        session_id=state.session_id,
        tool_name=decision.tool_name,
        idempotency_key=decision.idempotency_key or "",
        status="committed",
        side_effect_level=_result_side_effect_level(decision, result),
        output_ref=result.output_ref,
        summary=_result_summary(result),
    )


def should_record_idempotent_result(decision: ToolRiskDecision, result: ToolResult) -> bool:
    if not decision.idempotency_required or decision.idempotency_key is None:
        return False
    if not result.success:
        return False
    data = result.data if isinstance(result.data, dict) else {}
    if data.get("requires_confirmation") is True or _metadata_string(data.get("confirmation_id")):
        return False
    if isinstance(data.get("idempotency"), dict) and data["idempotency"].get("duplicate_suppressed") is True:
        return False
    return True


def _tool_confirmation_granted(request: UserRequest, tool_name: str) -> bool:
    confirmation = request.metadata.get("tool_confirmation")
    if not isinstance(confirmation, dict):
        return False
    if confirmation.get("confirmed") is not True:
        return False
    confirmed_tool = _metadata_string(confirmation.get("tool_name"))
    return confirmed_tool in {None, tool_name}


def _can_generate_idempotency_key(step_id: str | None) -> bool:
    return bool(_metadata_string(step_id))


def _generated_idempotency_key(
    *,
    tool_name: str,
    request: UserRequest,
    state: AgentState,
    step_id: str | None,
) -> str:
    task_state = request.metadata.get("realtime_task_state")
    task_id = task_state.get("task_id") if isinstance(task_state, dict) else None
    payload = {
        "user_id": state.user_id,
        "session_id": state.session_id,
        "run_id": state.run_id,
        "task_id": _metadata_string(task_id),
        "tool_name": tool_name,
        "step_id": _metadata_string(step_id),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"auto:{digest[:24]}"


def _result_summary(result: ToolResult) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    summary = _metadata_string(data.get("summary")) or _metadata_string(result.output_ref)
    return summary or "Tool execution completed."


def _result_side_effect_level(decision: ToolRiskDecision, result: ToolResult) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    return _metadata_string(data.get("side_effect_level") or data.get("effect_level")) or decision.side_effect_level


def _metadata_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clip(value: str, *, max_chars: int) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
