"""Controlled lifecycle events for Gateway session and run boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GATEWAY_LIFECYCLE_SCHEMA_VERSION = "gateway_lifecycle_event_v1"
GATEWAY_LIFECYCLE_SAFE_PAYLOAD_FIELDS = frozenset(
    {
        "active_count",
        "active_runs",
        "cancel_phase",
        "created",
        "disposition",
        "expects_reply",
        "global_queue_depth",
        "handled_by",
        "host",
        "limit",
        "log_dir",
        "max_active_runs",
        "newly_marked",
        "phase",
        "port",
        "queue_depth",
        "queue_reason",
        "queue_wait_ms",
        "reason",
        "resumed",
        "scope",
        "source",
        "status",
    }
)
GATEWAY_LIFECYCLE_SAFE_REASONS = frozenset(
    {
        "cancelled",
        "completed",
        "error",
        "interrupted_by_new_turn",
        "queue_overflow",
        "queue_wait_timeout",
        "run_deadline_expired",
        "semantic_interrupt",
        "session_closed",
    }
)


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


class JsonlGatewayLifecycleStore:
    """JSONL-backed prompt-safe Gateway lifecycle event store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: GatewayLifecycleEvent) -> None:
        record = gateway_lifecycle_record(event)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def gateway_lifecycle_record(event: GatewayLifecycleEvent) -> dict[str, Any]:
    """Return the durable prompt-safe JSONL projection for one Gateway event."""

    return {
        "schema_version": GATEWAY_LIFECYCLE_SCHEMA_VERSION,
        "created_at": _utc_now_text(),
        "component": "gateway",
        "event": _safe_token(event.type),
        "run_id": _safe_optional_identifier(event.run_id),
        "turn_id": _safe_optional_identifier(event.turn_id),
        "trace_id": _safe_optional_identifier(event.payload.get("trace_id")),
        "user_id": digest_gateway_identifier(event.user_id),
        "session_id": digest_gateway_identifier(event.session_id),
        "attributes": gateway_lifecycle_attributes(event.payload),
    }


def gateway_lifecycle_attributes(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project allowlisted scalar payload fields with prompt-safe values."""

    return {
        key: _gateway_payload_value(key, value)
        for key, value in payload.items()
        if key in GATEWAY_LIFECYCLE_SAFE_PAYLOAD_FIELDS and _is_scalar(value)
    }


def digest_gateway_identifier(value: str | None) -> str | None:
    """Return a stable short digest suitable for Gateway event correlation."""

    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


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


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_optional_identifier(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _safe_token(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return "_".join(str(value).split())


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _gateway_payload_value(key: str, value: Any) -> Any:
    if (
        key == "reason"
        and isinstance(value, str)
        and value not in GATEWAY_LIFECYCLE_SAFE_REASONS
    ):
        return "client_supplied"
    return value
