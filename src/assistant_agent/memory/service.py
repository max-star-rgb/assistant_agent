"""Single runtime-facing service for long-term memory."""

from __future__ import annotations

import hashlib
from threading import Event
from time import perf_counter
from datetime import datetime, timezone
from typing import Any

from assistant_agent.runtime.state import AgentState
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.models import CompletedTurn, SessionMemorySnapshot
from assistant_agent.memory.observability import (
    record_ingestion_finished,
    record_ingestion_queued,
    record_session_recall,
)
from assistant_agent.memory.session_snapshot import SessionMemorySnapshotStore
from assistant_agent.identity import RequestIdentity
from assistant_agent.providers.provider_errors import (
    ProviderSafetyPolicy,
    sanitize_error_message,
)
from assistant_agent.observability.trace_store import TraceStore
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME


_INGESTION_TEXT_POLICY = ProviderSafetyPolicy(
    max_message_chars=4000,
    max_detail_chars=4000,
    redact_absolute_paths=False,
)


class LongTermMemoryService:
    """Own session recall, frozen snapshots, and background Mem0 ingestion."""

    def __init__(
        self,
        *,
        client: Any,
        snapshot_store: SessionMemorySnapshotStore,
        ingestion_queue: MemoryIngestionQueue,
    ) -> None:
        self.client = client
        self.snapshot_store = snapshot_store
        self.ingestion_queue = ingestion_queue

    def initialize_session(
        self,
        *,
        identity: RequestIdentity,
        state: AgentState,
        trace_store: TraceStore | None,
        reset: bool = False,
    ) -> SessionMemorySnapshot:
        """Recall once and freeze the original Mem0 records for a session."""

        started = perf_counter()
        try:
            initialization = self.snapshot_store.resolve(
                identity,
                loader=lambda: self._recall_snapshot(identity),
                reset=reset,
            )
        except Exception as exc:
            snapshot = SessionMemorySnapshot(
                status="session_start_failed",
                error_codes=["memory_session_start_failed"],
            )
            self.snapshot_store.put(identity, snapshot)
            record_session_recall(
                trace_store=trace_store,
                state=state,
                status="failed",
                latency_ms=_elapsed_ms(started),
                error_codes=snapshot.error_codes,
                error=exc,
            )
            return snapshot
        snapshot = initialization.snapshot
        if initialization.status == "loaded":
            record_session_recall(
                trace_store=trace_store,
                state=state,
                status=snapshot.status,
                latency_ms=_elapsed_ms(started),
                memory_count=len(snapshot.memories),
                error_codes=snapshot.error_codes,
            )
        return snapshot

    def attach_session_snapshot(
        self,
        state: AgentState,
    ) -> SessionMemorySnapshot | None:
        """Attach a frozen snapshot to a turn without triggering recall."""

        snapshot = self.snapshot_store.get(_identity_from_state(state))
        state.session_memory_snapshot = snapshot
        return snapshot

    def enqueue_completed_turn(
        self,
        *,
        state: AgentState,
        trace_store: TraceStore | None,
    ) -> bool:
        """Submit a completed turn without delaying the foreground response."""

        skip_reason = _structured_ingestion_skip_reason(state)
        if skip_reason is not None:
            state.request.metadata["memory_ingestion"] = {
                "status": "skipped",
                "reason": skip_reason,
            }
            return False
        turn = self._completed_turn(state)
        if turn is None:
            state.request.metadata["memory_ingestion"] = {"status": "skipped"}
            return False
        queued = Event()
        submitted = self.ingestion_queue.submit(
            ordering_key=turn.ordering_key,
            callback=lambda: self._ingest(
                queued=queued,
                turn=turn,
                state=state,
                trace_store=trace_store,
            ),
        )
        if not submitted.accepted:
            state.request.metadata["memory_ingestion"] = {
                "status": "failed",
                "error_code": submitted.reason,
            }
            return False
        state.request.metadata["memory_ingestion"] = {
            "status": "queued",
            "pending_count": submitted.pending_count,
        }
        record_ingestion_queued(
            trace_store=trace_store,
            state=state,
            pending_count=submitted.pending_count,
        )
        queued.set()
        return True

    def clear_session(self, *, user_id: str, session_id: str) -> int:
        return self.snapshot_store.clear_session(
            user_id=user_id,
            session_id=session_id,
        )

    def clear_user(self, *, user_id: str, agent_id: str | None = None) -> int:
        return self.snapshot_store.clear_user(
            user_id=user_id,
            agent_id=agent_id,
        )

    def drain(self, *, timeout: float | None = None) -> bool:
        return self.ingestion_queue.drain(timeout=timeout)

    def close(self, *, timeout: float | None = None) -> bool:
        return self.ingestion_queue.close(timeout=timeout)

    def _recall_snapshot(
        self,
        identity: RequestIdentity,
    ) -> SessionMemorySnapshot:
        try:
            memories = self.client.recall_long_term_memory(identity)
        except Exception:
            return SessionMemorySnapshot(
                status="degraded",
                error_codes=["mem0_recall_failed"],
            )
        return SessionMemorySnapshot(memories=memories)

    def _completed_turn(self, state: AgentState) -> CompletedTurn | None:
        if not bool(getattr(self.client, "configured", True)):
            return None
        response = state.response
        if state.status != "completed" or response is None:
            return None
        user_text = sanitize_error_message(
            state.request.text,
            policy=_INGESTION_TEXT_POLICY,
        )
        assistant_text = sanitize_error_message(
            response.message,
            policy=_INGESTION_TEXT_POLICY,
        )
        if not user_text or not assistant_text:
            return None
        turn_index = str(
            state.request.metadata.get("conversation_turn_index") or "1"
        )
        source_turn = hashlib.sha256(
            f"{state.run_id}:{turn_index}".encode()
        ).hexdigest()[:24]
        return CompletedTurn(
            identity=_identity_from_state(state),
            user_text=user_text,
            assistant_text=assistant_text,
            occurred_at=datetime.now(timezone.utc),
            source_turn=source_turn,
        )

    def _ingest(
        self,
        *,
        queued: Event,
        turn: CompletedTurn,
        state: AgentState,
        trace_store: TraceStore | None,
    ) -> None:
        queued.wait()
        started = perf_counter()
        try:
            result = self.client.ingest_completed_turn(turn)
        except Exception as exc:
            record_ingestion_finished(
                trace_store=trace_store,
                state=state,
                status="failed",
                latency_ms=_elapsed_ms(started),
                error=exc,
            )
            return
        status = (
            "partial"
            if result.errors and result.accepted
            else "failed"
            if not result.accepted
            else "succeeded"
        )
        record_ingestion_finished(
            trace_store=trace_store,
            state=state,
            status=status,
            latency_ms=_elapsed_ms(started),
            memory_count=len(result.memory_ids),
            memory_ids=result.memory_ids,
            changes=getattr(result, "changes", None),
            source_turn=turn.source_turn,
            source_user_text=turn.user_text,
            source_assistant_text=turn.assistant_text,
            errors=result.errors,
        )


def _identity_from_state(state: AgentState) -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=state.user_id,
        agent_id=state.agent_id,
        session_id=state.session_id,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _structured_ingestion_skip_reason(state: AgentState) -> str | None:
    """Return a deterministic skip reason from governed ToolResult identity."""

    if state.tool_results and all(
        result.tool_name == VISUAL_REMINDER_MANAGE_TOOL_NAME
        for result in state.tool_results
    ):
        return "connection_scoped_visual_reminder"
    return None
