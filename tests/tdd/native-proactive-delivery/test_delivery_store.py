from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant_agent.runtime.proactive_delivery import (
    ProactiveDeliveryConflictError,
    ProactiveDeliveryOwnershipError,
    SQLiteProactiveDeliveryStore,
)
from assistant_agent.runtime.proactive_messages import ProactiveMessage


class ManualClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _message(
    message_id: str,
    *,
    user_id: str = "user-1",
    thread_id: str = "thread-1",
    mode: str = "durable",
    content: str = "content-sentinel",
) -> ProactiveMessage:
    return ProactiveMessage(
        message_id=message_id,
        user_id=user_id,
        session_id=thread_id,
        kind="system.notice",
        content=content,
        delivery_mode=mode,
        source_run_id="run-1",
        source_trace_id="trace-1",
    )


def _online_store(tmp_path):
    clock = ManualClock()
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3", clock=clock)
    store.register_presence(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ttl_seconds=45.0,
    )
    return store, clock


def test_durable_enqueue_is_idempotent_and_payload_drift_fails(tmp_path) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")

    first = store.enqueue(_message("message-1"))
    second = store.enqueue(_message("message-1"))

    assert second == first
    assert first.status == "queued"
    with pytest.raises(ProactiveDeliveryConflictError):
        store.enqueue(_message("message-1", content="changed-sentinel"))


def test_ephemeral_enqueue_uses_current_presence_snapshot(tmp_path) -> None:
    store, clock = _online_store(tmp_path)

    online = store.enqueue(_message("message-online", mode="connection_ephemeral"))
    assert online.status == "queued"

    clock.advance(46)
    offline = store.enqueue(_message("message-offline", mode="connection_ephemeral"))
    assert offline.status == "skipped_offline"
    assert offline.issue_code == "connection_offline"


def test_claim_is_serial_and_release_is_connection_scoped(tmp_path) -> None:
    store, _clock = _online_store(tmp_path)
    store.enqueue(_message("message-1"))
    store.enqueue(_message("message-2"))

    first = store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=True,
        lease_seconds=30.0,
    )
    blocked = store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=True,
        lease_seconds=30.0,
    )

    assert first is not None
    assert first.message.message_id == "message-1"
    assert first.status == "leased"
    assert blocked is None
    with pytest.raises(ProactiveDeliveryOwnershipError):
        store.release(
            message_id="message-1",
            connection_id="connection-other",
            issue_code="connection_lost",
        )
    released = store.release(
        message_id="message-1",
        connection_id="connection-1",
        issue_code="connection_lost",
    )
    assert released.status == "queued"
    assert released.issue_code == "connection_lost"


def test_ack_and_ephemeral_terminal_transitions_validate_identity(tmp_path) -> None:
    store, _clock = _online_store(tmp_path)
    store.enqueue(_message("durable-1"))
    store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=True,
        lease_seconds=30.0,
    )

    with pytest.raises(ProactiveDeliveryOwnershipError):
        store.acknowledge(
            message_id="durable-1",
            user_id="user-other",
            thread_id="thread-1",
            connection_id="connection-1",
        )
    acknowledged = store.acknowledge(
        message_id="durable-1",
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
    )
    assert acknowledged.status == "acknowledged"

    store.enqueue(_message("ephemeral-1", mode="connection_ephemeral"))
    store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=False,
        lease_seconds=30.0,
    )
    sent = store.mark_sent_unacknowledged(
        message_id="ephemeral-1",
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
    )
    assert sent.status == "sent_unacknowledged"


def test_missing_ack_capability_and_expired_lease_are_recoverable(tmp_path) -> None:
    store, clock = _online_store(tmp_path)
    store.enqueue(_message("message-1"))

    assert store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=False,
        lease_seconds=30.0,
    ) is None
    assert store.get("message-1").issue_code == "ack_capability_missing"

    claimed = store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=True,
        lease_seconds=30.0,
    )
    assert claimed is not None
    clock.advance(31)
    store.refresh_presence(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ttl_seconds=45.0,
    )
    reclaimed = store.claim_next(
        user_id="user-1",
        thread_id="thread-1",
        connection_id="connection-1",
        ack_capable=True,
        lease_seconds=30.0,
    )
    assert reclaimed is not None
    assert reclaimed.message.message_id == "message-1"
    assert reclaimed.attempt_count == 2


def test_unregister_presence_prevents_future_ephemeral_enqueue(tmp_path) -> None:
    store, _clock = _online_store(tmp_path)
    store.unregister_presence(thread_id="thread-1", connection_id="connection-1")

    record = store.enqueue(_message("message-1", mode="connection_ephemeral"))

    assert record.status == "skipped_offline"
