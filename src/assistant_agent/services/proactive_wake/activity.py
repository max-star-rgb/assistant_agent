"""Read-only user activity views for proactive wake attention policy."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.gateway.session import GatewaySessionManager
from assistant_agent.schemas.proactive_wake import WakeOwner


class UserActivityReader(Protocol):
    async def is_active(self, owner: WakeOwner) -> bool:
        raise NotImplementedError


class NullUserActivityReader:
    async def is_active(self, owner: WakeOwner) -> bool:
        return False


class GatewayUserActivityReader:
    def __init__(self, manager: GatewaySessionManager) -> None:
        self.manager = manager

    async def is_active(self, owner: WakeOwner) -> bool:
        return await self.manager.has_active_run(owner.user_id)
