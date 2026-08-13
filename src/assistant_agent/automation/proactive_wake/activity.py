"""Read-only user activity views for proactive wake attention policy."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.automation.proactive_wake.models import WakeOwner


class UserActivityReader(Protocol):
    async def is_active(self, owner: WakeOwner) -> bool:
        raise NotImplementedError


class NullUserActivityReader:
    async def is_active(self, owner: WakeOwner) -> bool:
        return False
