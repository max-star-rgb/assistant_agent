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

    def attach_continuation_snapshot(
        self,
        state: AgentState,
        *,
        origin_identity: RequestIdentity,
        origin_run_id: str,
        expected_memory_refs: tuple[tuple[str, str], ...],
    ) -> SessionMemorySnapshot | None:
        return self.host.attach_continuation_context(
            state,
            origin_identity=origin_identity,
            origin_run_id=origin_run_id,
            expected_memory_refs=expected_memory_refs,
        )

    def release_run_context(
        self,
        *,
        identity: RequestIdentity,
        run_id: str,
    ) -> bool:
        return self.host.release_run_context(
            identity=identity,
            run_id=run_id,
        )

    def release_thread_contexts(self, *, identity: RequestIdentity) -> int:
        return self.host.release_thread_contexts(identity=identity)

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

    def finalize_session(
        self,
        *,
        identity: RequestIdentity,
        reason: Literal[
            "normal", "reset", "expired", "shutdown", "plugin_replaced"
        ] = "normal",
        timeout: float | None = None,
    ) -> MemorySessionCloseResult:
        """Close Plugin-owned resources, then clear only Host-local state."""

        result = self.close_session(
            identity=identity,
            reason=reason,
            timeout=timeout,
        )
        if identity.session_id is not None:
            self.clear_session(
                user_id=identity.user_id,
                session_id=identity.session_id,
            )
        return result

    def reset_user_sessions(
        self,
        *,
        user_id: str,
        agent_id: str,
        timeout: float | None = None,
    ) -> int:
        """Close every Host-owned session for one user/agent before clearing."""

        runtime_keys = {
            record.runtime_identity_key
            for record in self.host.session_store.list_records()
            if record.runtime_identity_key[0] == user_id
            and record.runtime_identity_key[1] == agent_id
        }
        runtime_keys.update(
            retired.record.runtime_identity_key
            for retired in self.host.session_store.list_retired_records()
            if retired.record.runtime_identity_key[0] == user_id
            and retired.record.runtime_identity_key[1] == agent_id
        )
        closed = 0
        for record_user_id, record_agent_id, session_id in sorted(runtime_keys):
            result = self.finalize_session(
                identity=RequestIdentity.for_user(
                    user_id=record_user_id,
                    agent_id=record_agent_id,
                    session_id=session_id,
                ),
                reason="reset",
                timeout=timeout,
            )
            if result.status == "closed":
                closed += 1
        self.clear_user(user_id=user_id, agent_id=agent_id)
        return closed

    def clear_session(self, *, user_id: str, session_id: str) -> int:
        return self.host.clear_session(user_id=user_id, session_id=session_id)

    def clear_user(self, *, user_id: str, agent_id: str | None = None) -> int:
        return self.host.clear_user(user_id=user_id, agent_id=agent_id)

    def drain(self, *, timeout: float | None = None) -> bool:
        return self.host.drain(timeout=timeout)

    def close(self, *, timeout: float | None = None) -> bool:
        return self.host.close(timeout=timeout)
