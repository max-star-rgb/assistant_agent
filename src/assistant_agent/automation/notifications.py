"""Shared notification outbox boundary."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.automation.notification_models import NotificationEnvelope


class NotificationOutbox(Protocol):
    """Persist a notification request with idempotent identity."""

    def enqueue_notification(
        self,
        notification: NotificationEnvelope,
    ) -> NotificationEnvelope: ...
