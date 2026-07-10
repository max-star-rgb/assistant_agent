"""Shared realtime turn cancellation metadata contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel


RealtimeTurnCancelledBy = Literal[
    "interrupt",
    "run.cancel",
    "hangup",
    "disconnect",
    "deadline",
]
RealtimeTurnCancelPhase = Literal[
    "before_llm",
    "llm_streaming",
    "tool_running",
    "final_streaming",
    "tts_playing",
]

REALTIME_TURN_CANCELLATION_METADATA_KEY = "realtime_turn_cancellation"


class RealtimeTurnCancellationContract(BaseModel):
    """Prompt-safe cancellation facts shared across realtime turn boundaries."""

    cancelled_by: RealtimeTurnCancelledBy = "run.cancel"
    phase: RealtimeTurnCancelPhase = "final_streaming"
    stale_outputs: bool = True
    can_reuse_tool_result: bool = False
    speakable: bool = False


def build_realtime_turn_cancellation_metadata(
    metadata: Mapping[str, Any] | None = None,
    *,
    phase: str | None = None,
    cancelled_by: str | None = None,
    stale_outputs: bool = True,
    can_reuse_tool_result: bool = False,
    speakable: bool = False,
) -> dict[str, Any]:
    """Return metadata enriched with the realtime cancellation contract."""

    payload = dict(metadata or {})
    contract = RealtimeTurnCancellationContract(
        cancelled_by=_cancelled_by(cancelled_by or payload.get("cancel_source")),
        phase=_cancel_phase(phase or payload.get("cancel_phase")),
        stale_outputs=stale_outputs,
        can_reuse_tool_result=can_reuse_tool_result,
        speakable=speakable,
    )
    payload[REALTIME_TURN_CANCELLATION_METADATA_KEY] = contract.model_dump(mode="json")
    payload["stale_outputs"] = contract.stale_outputs
    payload["can_reuse_tool_result"] = contract.can_reuse_tool_result
    payload["speakable"] = contract.speakable
    return payload


def realtime_turn_cancellation_from_metadata(
    metadata: Mapping[str, Any] | None = None,
    *,
    phase: str | None = None,
) -> RealtimeTurnCancellationContract:
    """Read a realtime cancellation contract from metadata or derive one."""

    payload = dict(metadata or {})
    contract_payload = payload.get(REALTIME_TURN_CANCELLATION_METADATA_KEY)
    if isinstance(contract_payload, Mapping):
        contract = RealtimeTurnCancellationContract.model_validate(contract_payload)
        if phase is None:
            return contract
        return contract.model_copy(update={"phase": _cancel_phase(phase)})
    return RealtimeTurnCancellationContract(
        cancelled_by=_cancelled_by(payload.get("cancel_source")),
        phase=_cancel_phase(phase or payload.get("cancel_phase")),
        stale_outputs=bool(payload.get("stale_outputs", True)),
        can_reuse_tool_result=bool(payload.get("can_reuse_tool_result", False)),
        speakable=bool(payload.get("speakable", False)),
    )


def _cancelled_by(value: Any) -> RealtimeTurnCancelledBy:
    text = str(value or "").strip()
    if text == "gateway_interrupt":
        return "interrupt"
    if text == "gateway_hangup":
        return "hangup"
    if text == "gateway_disconnect":
        return "disconnect"
    if text == "deadline":
        return "deadline"
    return "run.cancel"


def _cancel_phase(value: Any) -> RealtimeTurnCancelPhase:
    text = str(value or "").strip()
    if text in {"pre_run", "pre_graph", "before_llm"}:
        return "before_llm"
    if text == "tts_playing":
        return "tts_playing"
    if "tool" in text:
        return "tool_running"
    if text in {"post_run", "gateway_output_gate", "gateway_exception", "final_streaming"}:
        return "final_streaming"
    return "llm_streaming"
