"""Shared notification outbox and delivery-observer boundaries."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.automation.notification_models import NotificationEnvelope


class NotificationOutbox(Protocol):
    """Persist a notification request with idempotent identity."""

    def enqueue_notification(
        self,
        notification: NotificationEnvelope,
    ) -> NotificationEnvelope: ...


class NotificationDeliveryObserver(Protocol):
    """Observe a persisted delivery transition without owning delivery."""

    def record_notification_delivery(
        self,
        notification: NotificationEnvelope,
    ) -> None: ...
