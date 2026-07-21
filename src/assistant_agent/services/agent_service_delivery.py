"""Prompt-safe delivery state and audit for the agent-service WebSocket."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol

from assistant_agent.services.identifiers import new_delivery_id


DEFAULT_AUDIT_PATH = Path(".data/agent_service_delivery.jsonl")


class AgentServiceDeliveryError(ValueError):
    """Recoverable delivery protocol error."""


class DeliveryAuditSink(Protocol):
    def append(self, delivery: "AgentServiceDelivery", event_type: str, **metadata) -> None: ...


@dataclass(frozen=True)
class AgentServiceDelivery:
    delivery_id: str
    session_digest: str
    chat_index_digest: str
    chat_index: str
    expects_ack: bool
    status: str = "accepted"
    run_id: str | None = None
    gateway_run_id: str | None = None
    assistant_run_id: str | None = None
    trace_id: str | None = None


class JsonlAgentServiceDeliveryAudit:
    def __init__(self, path: Path | str = DEFAULT_AUDIT_PATH) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def append(self, delivery: AgentServiceDelivery, event_type: str, **metadata) -> None:
        record = {
            "schema_version": "agent_service_delivery_v1",
            "delivery_id": delivery.delivery_id,
            "session_digest": delivery.session_digest,
            "chat_index_digest": delivery.chat_index_digest,
            "event_type": event_type,
            "status": delivery.status,
            "expects_ack": delivery.expects_ack,
            "run_id": delivery.run_id,
            "gateway_run_id": delivery.gateway_run_id,
            "assistant_run_id": delivery.assistant_run_id,
            "trace_id": delivery.trace_id,
            "created_at": datetime.now(UTC).isoformat(),
            **metadata,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


class AgentServiceDeliveryRegistry:
    def __init__(self, audit_sink: DeliveryAuditSink | None = None) -> None:
        self.audit_sink = audit_sink or JsonlAgentServiceDeliveryAudit()
        self._deliveries: dict[str, AgentServiceDelivery] = {}
        self._lock = Lock()

    def accept(self, session_id: str, chat_index: object, *, expects_ack: bool) -> AgentServiceDelivery:
        delivery = AgentServiceDelivery(
            delivery_id=new_delivery_id(),
            session_digest=_digest(session_id),
            chat_index_digest=_digest(str(chat_index)),
            chat_index=str(chat_index),
            expects_ack=expects_ack,
        )
        with self._lock:
            self._deliveries[delivery.delivery_id] = delivery
        self.audit_sink.append(delivery, "accepted")
        return delivery

    def get(self, delivery_id: str) -> AgentServiceDelivery | None:
        with self._lock:
            return self._deliveries.get(delivery_id)

    def mark_processing(self, delivery_id: str) -> AgentServiceDelivery:
        return self._transition(delivery_id, "processing")

    def mark_sent(
        self,
        delivery_id: str,
        *,
        run_id: str | None = None,
        gateway_run_id: str | None = None,
        assistant_run_id: str | None = None,
        trace_id: str | None = None,
    ) -> AgentServiceDelivery:
        return self._transition(
            delivery_id,
            "sent",
            run_id=run_id,
            gateway_run_id=gateway_run_id,
            assistant_run_id=assistant_run_id,
            trace_id=trace_id,
        )

    def mark_failed(
        self,
        delivery_id: str,
        *,
        error_code: str,
        run_id: str | None = None,
        gateway_run_id: str | None = None,
        assistant_run_id: str | None = None,
        trace_id: str | None = None,
        runtime_status: str | None = None,
        failure_source: str | None = None,
    ) -> AgentServiceDelivery:
        return self._transition(
            delivery_id,
            "failed",
            error_code=error_code,
            run_id=run_id,
            gateway_run_id=gateway_run_id,
            assistant_run_id=assistant_run_id,
            trace_id=trace_id,
            runtime_status=runtime_status,
            failure_source=failure_source,
        )

    def ack(self, delivery_id: str, *, chat_index: object) -> AgentServiceDelivery:
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise AgentServiceDeliveryError("unknown deliveryId")
            if current.chat_index != str(chat_index):
                raise AgentServiceDeliveryError("chatIndex mismatch")
            if current.status == "acked":
                raise AgentServiceDeliveryError("delivery already acknowledged")
            if current.status != "sent":
                raise AgentServiceDeliveryError("delivery is not awaiting acknowledgment")
            updated = replace(current, status="acked")
            self._deliveries[delivery_id] = updated
        self.audit_sink.append(updated, "acked")
        return updated

    def mark_disconnected(
        self,
        delivery_id: str,
        *,
        close_code: int | None = None,
        close_reason: str | None = None,
    ) -> AgentServiceDelivery:
        current = self._required(delivery_id)
        if current.status == "acked":
            return current
        status = "disconnected_before_ack" if current.status == "sent" else "disconnected_before_send"
        return self._transition(
            delivery_id,
            status,
            close_code=close_code,
            close_reason_category=_close_reason_category(close_reason),
        )

    def pending(self) -> list[AgentServiceDelivery]:
        with self._lock:
            return [
                item
                for item in self._deliveries.values()
                if item.status not in {"acked", "failed"}
                and not (item.status == "sent" and not item.expects_ack)
            ]

    def _required(self, delivery_id: str) -> AgentServiceDelivery:
        delivery = self.get(delivery_id)
        if delivery is None:
            raise AgentServiceDeliveryError("unknown deliveryId")
        return delivery

    def _transition(self, delivery_id: str, status: str, **metadata) -> AgentServiceDelivery:
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise AgentServiceDeliveryError("unknown deliveryId")
            legacy_run_id = metadata.pop("run_id", None)
            gateway_run_id = metadata.pop("gateway_run_id", None) or legacy_run_id
            assistant_run_id = metadata.pop("assistant_run_id", None)
            updated = replace(
                current,
                status=status,
                run_id=legacy_run_id or gateway_run_id or current.run_id,
                gateway_run_id=gateway_run_id or current.gateway_run_id,
                assistant_run_id=assistant_run_id or current.assistant_run_id,
                trace_id=metadata.pop("trace_id", None) or current.trace_id,
            )
            self._deliveries[delivery_id] = updated
        self.audit_sink.append(updated, status, **metadata)
        return updated


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _close_reason_category(reason: str | None) -> str:
    return "client_disconnect" if reason else "connection_closed"
