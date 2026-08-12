"""Process-local Agent-Service delivery leases for durable notifications."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from assistant_agent.automation.notification_models import (
    DeliveryResult,
    NotificationEnvelope,
    NotificationOwner,
)


NotificationSender = Callable[[NotificationEnvelope], Awaitable[DeliveryResult]]


@dataclass(frozen=True)
class AgentServiceNotificationBinding:
    subscriber_id: str
    sender: NotificationSender


class AgentServiceNotificationHub:
    """Route a durable notification to the latest online owner connection."""

    def __init__(self) -> None:
        self._bindings: dict[
            tuple[str, str], AgentServiceNotificationBinding
        ] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        owner: NotificationOwner,
        subscriber_id: str,
        sender: NotificationSender,
    ) -> None:
        binding = AgentServiceNotificationBinding(
            subscriber_id=subscriber_id,
            sender=sender,
        )
        async with self._lock:
            self._bindings[_owner_key(owner)] = binding

    async def unregister(
        self,
        *,
        owner: NotificationOwner,
        subscriber_id: str,
    ) -> None:
        async with self._lock:
            key = _owner_key(owner)
            binding = self._bindings.get(key)
            if binding is not None and binding.subscriber_id == subscriber_id:
                self._bindings.pop(key, None)

    async def is_available(self, owner: NotificationOwner) -> bool:
        async with self._lock:
            return _owner_key(owner) in self._bindings

    async def publish(self, notification: NotificationEnvelope) -> DeliveryResult:
        async with self._lock:
            binding = self._bindings.get(_owner_key(notification.owner))
        if binding is None:
            return DeliveryResult(
                accepted=False,
                error_code="recipient_offline",
            )
        return await binding.sender(notification)


class AgentServiceNotificationTransport:
    def __init__(self, hub: AgentServiceNotificationHub) -> None:
        self.hub = hub

    async def is_available(self, owner: NotificationOwner) -> bool:
        return await self.hub.is_available(owner)

    async def send(self, notification: NotificationEnvelope) -> DeliveryResult:
        if notification.channel != "agent_service":
            return DeliveryResult(
                accepted=False,
                error_code="unsupported_notification_channel",
            )
        return await self.hub.publish(notification)


def _owner_key(owner: NotificationOwner) -> tuple[str, str]:
    return owner.user_id, owner.agent_id


_agent_service_notification_hub = AgentServiceNotificationHub()


def get_agent_service_notification_hub() -> AgentServiceNotificationHub:
    return _agent_service_notification_hub
