"""Durable delivery worker for proactive wake notifications."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Protocol

from assistant_agent.schemas.proactive_wake import (
    DeliveryResult,
    NotificationEnvelope,
    utc_now,
)
from assistant_agent.services.proactive_wake.activity import (
    NullUserActivityReader,
    UserActivityReader,
)
from assistant_agent.services.proactive_wake.store import (
    SQLiteProactiveWakeStore,
    StaleNotificationLeaseError,
)


class ProactiveNotificationTransport(Protocol):
    async def send(self, notification: NotificationEnvelope) -> DeliveryResult:
        raise NotImplementedError


class MockProactiveNotificationTransport:
    def __init__(self, results: list[DeliveryResult] | None = None) -> None:
        self.results = list(results or [])
        self.sent: list[NotificationEnvelope] = []

    async def send(self, notification: NotificationEnvelope) -> DeliveryResult:
        self.sent.append(notification)
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(
            accepted=True,
            provider_message_id=f"mock:{notification.delivery_id}",
        )


class NotificationDeliveryWorker:
    def __init__(
        self,
        *,
        store: SQLiteProactiveWakeStore,
        transport: ProactiveNotificationTransport,
        activity_reader: UserActivityReader | None = None,
        now_fn: Callable[[], datetime] = utc_now,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.transport = transport
        self.activity_reader = activity_reader or NullUserActivityReader()
        self.now_fn = now_fn
        self.max_attempts = max_attempts

    async def drain_once(self, *, limit: int = 20) -> list[NotificationEnvelope]:
        now = self.now_fn()
        claimed = self.store.claim_due_notifications(now=now, limit=limit)
        completed = []
        for notification in claimed:
            lease_until = notification.lease_until
            if lease_until is None:  # pragma: no cover - defensive store boundary
                raise RuntimeError("claimed notification has no lease")
            try:
                item_now = self.now_fn()
                if notification.expires_at <= item_now:
                    completed.append(
                        self.store.mark_notification_expired(
                            notification.delivery_id,
                            now=item_now,
                            expected_lease_until=lease_until,
                        )
                    )
                    continue
                if notification.attempt_count >= self.max_attempts:
                    completed.append(
                        self.store.mark_notification_attempts_exhausted(
                            notification.delivery_id,
                            now=item_now,
                            expected_lease_until=lease_until,
                        )
                    )
                    continue
                if await self.activity_reader.is_active(notification.owner):
                    item_now = self.now_fn()
                    completed.append(
                        self.store.defer_notification(
                            notification.delivery_id,
                            available_at=item_now + timedelta(seconds=60),
                            reason_code="active_conversation",
                            now=item_now,
                            expected_lease_until=lease_until,
                        )
                    )
                    continue

                in_flight, attempt_id = self.store.begin_notification_attempt(
                    notification.delivery_id,
                    expected_lease_until=lease_until,
                    now=self.now_fn(),
                )
                try:
                    result = await self.transport.send(in_flight)
                except Exception:
                    result = DeliveryResult(
                        accepted=False,
                        error_code="transport_exception",
                    )
                item_now = self.now_fn()
                if result.accepted:
                    completed.append(
                        self.store.mark_notification_sent(
                            notification.delivery_id,
                            provider_message_id=result.provider_message_id,
                            now=item_now,
                            expected_lease_until=lease_until,
                            attempt_id=attempt_id,
                        )
                    )
                    continue

                retry_at = None
                if in_flight.attempt_count < self.max_attempts:
                    delay_s = min(300, 5 * 2 ** (in_flight.attempt_count - 1))
                    retry_at = item_now + timedelta(seconds=delay_s)
                completed.append(
                    self.store.mark_notification_failed(
                        notification.delivery_id,
                        error_code=result.error_code or "delivery_rejected",
                        retry_at=retry_at,
                        now=item_now,
                        max_attempts=self.max_attempts,
                        expected_lease_until=lease_until,
                        attempt_id=attempt_id,
                    )
                )
            except StaleNotificationLeaseError:
                continue
        return completed
