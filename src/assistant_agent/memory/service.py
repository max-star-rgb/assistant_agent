"""Compatibility facade for the governed Memory Plugin Host."""

from __future__ import annotations

from typing import Any, Literal

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.plugins.contracts import MemorySessionCloseResult
from assistant_agent.memory.plugins.host import MemoryPluginHost
from assistant_agent.observability.trace_store import TraceStore
from assistant_agent.runtime.state import AgentState


class LongTermMemoryService:
    """Preserve the Runtime-facing API while delegating all work to Host."""

    def __init__(self, *, host: MemoryPluginHost) -> None:
        if not isinstance(host, MemoryPluginHost):
            raise TypeError("host must be a MemoryPluginHost")
        self.host = host

    @property
    def ingestion_queue(self) -> MemoryIngestionQueue:
        """Expose the Host-owned queue for existing lifecycle diagnostics."""

        return self.host.ingestion_queue

    def initialize_session(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        trace_store: TraceStore | None,
        reset: bool = False,
    ) -> SessionMemorySnapshot:
        return self.host.open_session(
            identity=identity,
            state=state,
            trace_store=trace_store,
            reset=reset,
        )

    def prepare_context(
        self,
        *,
        state: AgentState,
        trace_store: TraceStore | None,
        cancel_token: Any | None,
    ) -> SessionMemorySnapshot:
        return self.host.prepare_context(
            state=state,
            trace_store=trace_store,
            cancel_token=cancel_token,
        )

    def attach_session_snapshot(
        self,
        state: AgentState,
    ) -> SessionMemorySnapshot | None:
        return self.host.attach_frozen_context(state)

    def enqueue_completed_turn(
        self,
        *,
        state: AgentState,
        trace_store: TraceStore | None,
    ) -> bool:
        return self.host.schedule_ingestion(
            state=state,
            trace_store=trace_store,
        )

    def close_session(
        self,
        *,
        identity: RequestIdentity,
        reason: Literal[
            "normal", "reset", "expired", "shutdown", "plugin_replaced"
        ] = "normal",
        timeout: float | None = None,
    ) -> MemorySessionCloseResult:
        return self.host.close_session(
            identity=identity,
            reason=reason,
            timeout=timeout,
        )

    def clear_session(self, *, user_id: str, session_id: str) -> int:
        return self.host.clear_session(user_id=user_id, session_id=session_id)

    def clear_user(self, *, user_id: str, agent_id: str | None = None) -> int:
        return self.host.clear_user(user_id=user_id, agent_id=agent_id)

    def drain(self, *, timeout: float | None = None) -> bool:
        return self.host.drain(timeout=timeout)

    def close(self, *, timeout: float | None = None) -> bool:
        return self.host.close(timeout=timeout)
