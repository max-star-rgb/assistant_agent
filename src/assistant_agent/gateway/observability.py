"""Controlled lifecycle events for Gateway session and run boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GatewayLifecycleEvent:
    """Prompt-safe lifecycle event emitted by Gateway boundary code."""

    type: str
    user_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


GatewayLifecycleSink = Callable[[GatewayLifecycleEvent], None]


def emit_gateway_lifecycle_event(
    sink: GatewayLifecycleSink | None,
    *,
    type: str,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Emit a lifecycle event without letting observer failures affect Gateway."""

    if sink is None:
        return
    try:
        sink(
            GatewayLifecycleEvent(
                type=type,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                payload=dict(payload or {}),
            )
        )
    except Exception:
        return
