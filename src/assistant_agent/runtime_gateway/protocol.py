"""Wire protocol helpers for assistant runtime gateway integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, TypedDict


ProtocolVersion = Literal[1]

CALL_INCOMING = "call.incoming"
CALL_READY = "call.ready"
CALL_HANGUP = "call.hangup"
CALL_HANGUP_ACK = "call.hangup_ack"
CONFIG_UPDATE = "config.update"


class Frame(TypedDict, total=False):
    v: ProtocolVersion
    type: str
    session_id: str
    turn_id: str
    run_id: str
    user_id: str
    payload: Any
    reason: str
    error: Any


def frame(
    *,
    type: str,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
    payload: Any = None,
    reason: Optional[str] = None,
    error: Any = None,
    v: ProtocolVersion = 1,
) -> Frame:
    f: Frame = {"v": v, "type": type}
    if session_id is not None:
        f["session_id"] = session_id
    if turn_id is not None:
        f["turn_id"] = turn_id
    if run_id is not None:
        f["run_id"] = run_id
    if user_id is not None:
        f["user_id"] = user_id
    if payload is not None:
        f["payload"] = payload
    if reason is not None:
        f["reason"] = reason
    if error is not None:
        f["error"] = error
    return f


@dataclass(frozen=True)
class RunEndReason:
    value: Literal["completed", "cancelled", "error"]


SUPPORTED_MODALITIES = frozenset({"text"})
