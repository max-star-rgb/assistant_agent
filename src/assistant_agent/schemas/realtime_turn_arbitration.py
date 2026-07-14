"""Prompt-safe contracts for realtime semantic turn arbitration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


REALTIME_TURN_ARBITRATION_SCHEMA_VERSION = "realtime_turn_arbitration_v1"
REALTIME_TURN_ARBITRATION_METADATA_KEY = "realtime_turn_arbitration"

RealtimeTurnDisposition = Literal[
    "FOLLOWUP",
    "CANCEL_ONLY",
    "REVISE_ACTIVE",
    "REPLACE_ACTIVE",
    "ACK_NOOP",
    "UNCERTAIN",
]
RealtimeTurnArbitrationSource = Literal[
    "semantic_llm",
    "deterministic_fallback",
]
RealtimeTurnRevisionType = Literal[
    "add_constraint",
    "replace_constraint",
    "change_goal",
    "cancel_goal",
    "confirm",
    "clarify",
]

_DISPOSITIONS = {
    "FOLLOWUP",
    "CANCEL_ONLY",
    "REVISE_ACTIVE",
    "REPLACE_ACTIVE",
    "ACK_NOOP",
    "UNCERTAIN",
}
_REVISE_TYPES = {"add_constraint", "replace_constraint", "confirm", "clarify"}
_REASON_CODE_PATTERN = re.compile(r"[^a-z0-9_]+")
_TASK_STATE_CONSTRAINT_LIMIT = 6
_TASK_STATE_CONSTRAINT_CHARS = 320
_TASK_STATE_PENDING_TEXT_CHARS = 128
_TASK_STATE_TTS_STATES = {"idle", "speaking", "interrupted", "superseded"}


class RealtimeTurnArbitrationRequest(BaseModel):
    """Bounded input for one independent semantic arbitration call."""

    schema_version: str = REALTIME_TURN_ARBITRATION_SCHEMA_VERSION
    decision_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    expected_run_id: str = Field(min_length=1, max_length=256)
    utterance: str = Field(min_length=1, max_length=1200)
    language: str | None = Field(default=None, max_length=32)
    task_state: dict[str, Any] = Field(default_factory=dict)


class RealtimeTurnArbitrationDecision(BaseModel):
    """Validated disposition returned to the Gateway control plane."""

    schema_version: str = REALTIME_TURN_ARBITRATION_SCHEMA_VERSION
    decision_id: str = Field(min_length=1, max_length=128)
    source: RealtimeTurnArbitrationSource
    disposition: RealtimeTurnDisposition
    revision_type: RealtimeTurnRevisionType | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=96)
    expected_run_id: str = Field(min_length=1, max_length=256)
    latency_ms: int = Field(default=0, ge=0)
    fallback_reason: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def validate_disposition_revision_pair(self) -> RealtimeTurnArbitrationDecision:
        """Reject contradictory decisions at every construction boundary."""

        if self.disposition == "REVISE_ACTIVE":
            if self.revision_type not in _REVISE_TYPES:
                raise ValueError("revision_type is invalid for REVISE_ACTIVE")
        elif self.disposition == "REPLACE_ACTIVE":
            if self.revision_type != "change_goal":
                raise ValueError("revision_type must be change_goal for REPLACE_ACTIVE")
        elif self.disposition == "CANCEL_ONLY":
            if self.revision_type != "cancel_goal":
                raise ValueError("revision_type must be cancel_goal for CANCEL_ONLY")
        elif self.revision_type is not None:
            raise ValueError(f"revision_type must be omitted for {self.disposition}")
        return self


def prompt_safe_arbitration_task_state(raw: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Project task state to the small, bounded semantic-control contract."""

    task_state = raw if isinstance(raw, Mapping) else {}
    constraints_value = task_state.get("constraints")
    constraints = []
    if isinstance(constraints_value, list):
        constraints = [
            _clip_string(value, max_chars=_TASK_STATE_CONSTRAINT_CHARS)
            for value in constraints_value[-_TASK_STATE_CONSTRAINT_LIMIT:]
            if isinstance(value, str) and value.strip()
        ]

    pending_tool_value = task_state.get("pending_tool")
    pending_tool: dict[str, Any] | None = None
    if isinstance(pending_tool_value, Mapping):
        pending_tool = {}
        for key in ("tool_name", "status", "current_step"):
            value = pending_tool_value.get(key)
            if isinstance(value, str) and value.strip():
                pending_tool[key] = _clip_string(
                    value,
                    max_chars=_TASK_STATE_PENDING_TEXT_CHARS,
                )
        requires_confirmation = pending_tool_value.get("requires_confirmation")
        if isinstance(requires_confirmation, bool):
            pending_tool["requires_confirmation"] = requires_confirmation
        if not pending_tool:
            pending_tool = None

    tts_state = str(task_state.get("tts_state") or "idle").strip().lower()
    if tts_state not in _TASK_STATE_TTS_STATES:
        tts_state = "idle"
    committed_side_effect_count = task_state.get("committed_side_effect_count", 0)
    if isinstance(committed_side_effect_count, bool) or not isinstance(
        committed_side_effect_count,
        int,
    ):
        committed_side_effect_count = 0

    return {
        "objective": _clip_string(task_state.get("objective"), max_chars=1200),
        "constraints": constraints,
        "pending_tool": pending_tool,
        "tts_state": tts_state,
        "committed_side_effect_count": min(
            1_000_000,
            max(0, committed_side_effect_count),
        ),
    }


def uncertain_arbitration_decision(
    request: RealtimeTurnArbitrationRequest,
    *,
    source: RealtimeTurnArbitrationSource = "deterministic_fallback",
    fallback_reason: str,
    reason_code: str = "uncertain",
    confidence: float = 0.0,
    latency_ms: int = 0,
) -> RealtimeTurnArbitrationDecision:
    """Return the conservative non-cancelling decision."""

    return RealtimeTurnArbitrationDecision(
        decision_id=request.decision_id,
        source=source,
        disposition="UNCERTAIN",
        revision_type=None,
        confidence=max(0.0, min(1.0, float(confidence))),
        reason_code=_reason_code(reason_code),
        expected_run_id=request.expected_run_id,
        latency_ms=max(0, int(latency_ms)),
        fallback_reason=_fallback_reason(fallback_reason),
    )


def normalize_arbitration_decision(
    raw: Mapping[str, Any] | Any,
    *,
    request: RealtimeTurnArbitrationRequest,
    min_confidence: float,
    source: RealtimeTurnArbitrationSource,
    latency_ms: int = 0,
) -> RealtimeTurnArbitrationDecision:
    """Validate model output and rebind every trusted identity field."""

    if not isinstance(raw, Mapping):
        return uncertain_arbitration_decision(
            request,
            source=source,
            fallback_reason="invalid_model_output",
            latency_ms=latency_ms,
        )
    disposition = str(raw.get("disposition") or "").strip().upper()
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return uncertain_arbitration_decision(
            request,
            source=source,
            fallback_reason="invalid_model_output",
            reason_code=str(raw.get("reason_code") or "invalid_model_output"),
            latency_ms=latency_ms,
        )
    if disposition not in _DISPOSITIONS or not 0.0 <= confidence <= 1.0:
        return uncertain_arbitration_decision(
            request,
            source=source,
            fallback_reason="invalid_model_output",
            reason_code=str(raw.get("reason_code") or "invalid_model_output"),
            confidence=confidence if 0.0 <= confidence <= 1.0 else 0.0,
            latency_ms=latency_ms,
        )

    reason_code = _reason_code(raw.get("reason_code"))
    if confidence < min_confidence:
        return uncertain_arbitration_decision(
            request,
            source=source,
            fallback_reason="low_confidence",
            reason_code=reason_code,
            confidence=confidence,
            latency_ms=latency_ms,
        )

    revision_type = _normalized_revision_type(
        disposition,
        raw.get("revision_type"),
    )
    fallback_reason = None
    if disposition == "UNCERTAIN":
        fallback_reason = _fallback_reason(raw.get("fallback_reason") or "model_uncertain")
    try:
        return RealtimeTurnArbitrationDecision(
            decision_id=request.decision_id,
            source=source,
            disposition=disposition,
            revision_type=revision_type,
            confidence=confidence,
            reason_code=reason_code,
            expected_run_id=request.expected_run_id,
            latency_ms=max(0, int(latency_ms)),
            fallback_reason=fallback_reason,
        )
    except ValidationError:
        return uncertain_arbitration_decision(
            request,
            source=source,
            fallback_reason="invalid_model_output",
            reason_code=reason_code,
            confidence=confidence,
            latency_ms=latency_ms,
        )


def _normalized_revision_type(
    disposition: str,
    value: Any,
) -> RealtimeTurnRevisionType | None:
    if disposition == "CANCEL_ONLY":
        return "cancel_goal"
    if disposition == "REPLACE_ACTIVE":
        return "change_goal"
    if disposition == "REVISE_ACTIVE":
        candidate = str(value or "").strip().lower()
        if candidate in _REVISE_TYPES:
            return candidate  # type: ignore[return-value]
        return "add_constraint"
    return None


def _reason_code(value: Any) -> str:
    text = str(value or "unspecified").strip().lower()
    normalized = _REASON_CODE_PATTERN.sub("_", text).strip("_")
    return (normalized or "unspecified")[:96]


def _fallback_reason(value: Any) -> str:
    return _reason_code(value)


def _clip_string(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if len(text) <= max_chars else text[:max_chars]
