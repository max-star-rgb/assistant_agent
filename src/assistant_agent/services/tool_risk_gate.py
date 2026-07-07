"""Runtime risk gate and idempotency ledger for tool execution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult, ToolSideEffectPolicy
from assistant_agent.tools.registry import tool_side_effect_policy


TOOL_RISK_GATE_SCHEMA_VERSION = "tool_risk_gate_v1"
ToolRiskGateLevel = Literal["auto", "soft_gate", "hard_gate", "block"]
ToolIdempotencyStatus = Literal["committed", "pending_confirmation"]

_REALTIME_SOURCES = {
    "gateway_websocket",
    "realtime_agent_backend",
    "realtime_media_websocket",
    "phone_runtime",
}
_TOOL_OWNED_CONFIRMATION_TOOLS = {"memory", "memory_save"}


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


def risk_gate_level_for_policy(policy: ToolSideEffectPolicy) -> ToolRiskGateLevel:
    """Map ToolSpec side-effect policy onto a runtime gate level."""

    if policy.level in {"none", "local_read", "external_read"} and not policy.requires_confirmation:
        return "auto"
    if policy.level == "compensatable" and not policy.requires_confirmation:
        return "soft_gate"
    return "hard_gate"


def evaluate_tool_risk(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    request: UserRequest,
    state: AgentState,
    step_id: str | None,
) -> ToolRiskDecision:
    """Return the runtime risk/idempotency decision for a tool call."""

    policy = tool_side_effect_policy(tool_name)
    level = risk_gate_level_for_policy(policy)
    enabled = _risk_gate_enabled(request)
    supplied_key = _metadata_string(tool_input.get("idempotency_key"))

    if level == "auto":
        return ToolRiskDecision(
            tool_name=tool_name,
            level=level,
            side_effect_level=policy.level,
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
            side_effect_level=policy.level,
            enabled=enabled,
            allow_execute=True,
            requires_confirmation=False,
            reason="compensatable_side_effect",
            idempotency_required=True,
            idempotency_key=key,
            idempotency_generated=generated,
        )

    confirmation_owned_by_tool = tool_name in _TOOL_OWNED_CONFIRMATION_TOOLS
    allow_execute = not enabled or confirmation_owned_by_tool
    return ToolRiskDecision(
        tool_name=tool_name,
        level=level,
        side_effect_level=policy.level,
        enabled=enabled,
        allow_execute=allow_execute,
        requires_confirmation=True,
        confirmation_kind=policy.confirmation_kind or "tool_execution",
        reason="tool_owned_confirmation" if confirmation_owned_by_tool else "confirmation_required",
        idempotency_required=False,
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

    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={
            "status": "confirmation_required",
            "summary": "Tool execution requires user confirmation before continuing.",
            "requires_confirmation": True,
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
    if decision.level == "auto" or decision.idempotency_key is None:
        return False
    if not result.success:
        return False
    data = result.data if isinstance(result.data, dict) else {}
    if data.get("requires_confirmation") is True or _metadata_string(data.get("confirmation_id")):
        return False
    if isinstance(data.get("idempotency"), dict) and data["idempotency"].get("duplicate_suppressed") is True:
        return False
    return True


def _risk_gate_enabled(request: UserRequest) -> bool:
    metadata = request.metadata
    if metadata.get("tool_risk_gate_enabled") is True:
        return True
    source = _metadata_string(metadata.get("source"))
    if source in _REALTIME_SOURCES:
        return True
    if isinstance(metadata.get("gateway"), dict):
        return True
    realtime = metadata.get("realtime")
    if isinstance(realtime, dict):
        return True
    return isinstance(metadata.get("realtime_task_state"), dict)


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
