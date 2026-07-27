"""Realtime backend protocol for assistant_agent integrations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from assistant_agent.gateway.runtime_types import (
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
    RealtimeBackendCapabilities,
    RealtimeCancelToken,
)


RealtimeEventSink = Callable[[RealtimeAgentEvent], Awaitable[None]]


class RealtimeAgentBackend(Protocol):
    """Neutral interface implemented by realtime-capable agent backends."""

    @property
    def capabilities(self) -> RealtimeBackendCapabilities:
        """Return the backend's realtime capability declaration."""

    async def run_turn(
        self,
        request: RealtimeAgentRequest,
        *,
        event_sink: RealtimeEventSink | None = None,
        cancel_token: RealtimeCancelToken | None = None,
    ) -> RealtimeAgentResult:
        """Run one realtime agent turn."""
