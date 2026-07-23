"""Application runtime boundary for product entry layers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory_snapshot import MemoryStorageSnapshot
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.sessions import SessionCreate, SessionList, SessionRecord
from assistant_agent.services.assistant_run_service import (
    AssistantRunArtifacts,
    clear_conversation_history,
    clear_user_conversation_history,
    get_default_conversation_store,
    run_assistant_request,
    runtime_info,
)
from assistant_agent.services.memory_audit import MemoryAuditService
from assistant_agent.services.memory_snapshot import MemorySnapshotService
from assistant_agent.services.trace_query import TraceQueryService


class AssistantRuntimeApp:
    """Product entry boundary over the internal AgentGraphRuntime."""

    def __init__(self, runtime_factory: Callable[[], AgentGraphRuntime]) -> None:
        self._runtime_factory = runtime_factory

    @property
    def runtime(self) -> AgentGraphRuntime:
        return self._runtime_factory()

    @property
    def config(self) -> ProviderConfig:
        return self.runtime.config

    def run_request(self, request: UserRequest, **kwargs: Any) -> AssistantRunArtifacts:
        return run_assistant_request(request, runtime=self.runtime, **kwargs)

    def run_query(
        self,
        query: str,
        *,
        image_refs: list[str] | None = None,
        video_refs: list[str] | None = None,
        user_id: str = "demo_user",
        session_id: str = "demo_session",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AssistantRunArtifacts:
        return self.run_request(
            UserRequest(
                user_id=user_id,
                session_id=session_id,
                text=query,
                image_ids=list(image_refs or []),
                video_ids=list(video_refs or []),
                metadata=metadata or {"source": "assistant_runtime_app"},
            ),
            **kwargs,
        )

    def runtime_info(self) -> dict[str, Any]:
        return runtime_info(self.config)

    def trace_query(self) -> TraceQueryService:
        return TraceQueryService(self.runtime.trace_store)

    def memory_audit_service(self) -> MemoryAuditService:
        runtime = self.runtime
        return MemoryAuditService(runtime.memory_manager, config=runtime.config)

    def memory_snapshot_service(self) -> MemorySnapshotService:
        runtime = self.runtime
        conversation_store = get_default_conversation_store(runtime.config)
        return MemorySnapshotService(
            memory_manager=runtime.memory_manager,
            session_store=runtime.session_store,
            conversation_store=conversation_store,
            storage=MemoryStorageSnapshot(
                memory_store=type(runtime.memory_store).__name__,
                session_store=type(runtime.session_store).__name__,
                conversation_store=type(conversation_store).__name__,
                checkpointer=type(runtime.checkpointer).__name__ if runtime.checkpointer is not None else "none",
            ),
            config=runtime.config,
        )

    def create_session(
        self,
        session: SessionCreate,
        *,
        identity: RequestIdentity | None = None,
    ) -> SessionRecord:
        runtime = self.runtime
        record = runtime.session_store.create(session)
        resolved = identity or RequestIdentity.for_user(user_id=record.user_id)
        runtime.initialize_session_memory(
            resolved.model_copy(
                update={
                    "user_id": record.user_id,
                    "session_id": record.session_id,
                }
            )
        )
        return record

    def list_sessions(self, user_id: str) -> SessionList:
        sessions = self.runtime.session_store.list_by_user(user_id)
        return SessionList(user_id=user_id, total=len(sessions), sessions=sessions)

    def get_session(self, user_id: str, session_id: str) -> SessionRecord | None:
        return self.runtime.session_store.get(user_id, session_id)

    def delete_session(self, user_id: str, session_id: str) -> bool:
        runtime = self.runtime
        deleted = runtime.session_store.delete(user_id, session_id)
        runtime.session_memory_context_store.clear_session(
            user_id=user_id,
            session_id=session_id,
        )
        if deleted:
            clear_conversation_history(user_id, session_id, config=runtime.config)
        return deleted

    def delete_user_runtime_data(self, user_id: str) -> dict[str, int]:
        runtime = self.runtime
        memory_items = runtime.memory_manager.list_by_user(user_id)
        runtime.memory_manager.clear_user(user_id)
        run_history_deleted = runtime.run_history.delete_by_user(user_id) if runtime.run_history is not None else 0
        trace_deleted = runtime.trace_store.delete_by_user(user_id)
        conversation_sessions_deleted = clear_user_conversation_history(user_id, config=runtime.config)
        session_records_deleted = runtime.session_store.delete_by_user(user_id)
        runtime.session_memory_context_store.clear_user(user_id=user_id)
        return {
            "memory_items": len(memory_items),
            "run_history_records": run_history_deleted,
            "trace_events": trace_deleted,
            "conversation_sessions": conversation_sessions_deleted,
            "session_records": session_records_deleted,
        }
