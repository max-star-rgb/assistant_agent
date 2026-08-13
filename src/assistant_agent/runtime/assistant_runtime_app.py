"""Application runtime boundary for product entry layers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.session_models import (
    SessionCreate,
    SessionList,
    SessionRecord,
)
from assistant_agent.runtime.assistant_run_service import (
    AssistantRunArtifacts,
    clear_conversation_history,
    clear_user_conversation_history,
    run_assistant_request,
    run_assistant_request_stream,
    runtime_info,
)
from assistant_agent.runtime.event_stream import AgentRunStream
from assistant_agent.runtime.graph_time_travel import (
    GraphCheckpointSelector,
    GraphCheckpointSummary,
    GraphForkRequest,
    GraphReplayRequest,
)
from assistant_agent.runtime.state import AgentState
from assistant_agent.observability.trace_query import TraceQueryService


class AssistantRuntimeApp:
    """Product entry boundary over the internal AgentGraphRuntime."""

    def __init__(self, runtime_factory: Callable[[], AgentGraphRuntime]) -> None:
        self._runtime_factory = runtime_factory
        self._runtime = runtime_factory()

    @property
    def runtime(self) -> AgentGraphRuntime:
        return self._runtime

    @property
    def config(self) -> ProviderConfig:
        return self.runtime.config

    def run_request(self, request: UserRequest, **kwargs: Any) -> AssistantRunArtifacts:
        return run_assistant_request(request, runtime=self.runtime, **kwargs)

    def run_request_stream(
        self,
        request: UserRequest,
        **kwargs: Any,
    ) -> AgentRunStream[AssistantRunArtifacts]:
        return run_assistant_request_stream(request, runtime=self.runtime, **kwargs)

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

    async def list_turn_history(
        self,
        owner: RequestIdentity,
        *,
        limit: int,
        before: GraphCheckpointSelector | None = None,
    ) -> tuple[GraphCheckpointSummary, ...]:
        """List safe checkpoints from the process-owned Runtime graph."""

        return await self.runtime.alist_history(owner, limit=limit, before=before)

    async def replay_turn(
        self,
        owner: RequestIdentity,
        request: GraphReplayRequest,
        *,
        run_id: str,
    ) -> AgentState:
        """Replay one owned checkpoint on the process-owned Runtime graph."""

        return await self.runtime.areplay_state(owner, request, run_id=run_id)

    async def fork_turn(
        self,
        owner: RequestIdentity,
        request: GraphForkRequest,
        *,
        run_id: str,
    ) -> AgentState:
        """Fork one owned checkpoint on the process-owned Runtime graph."""

        return await self.runtime.afork_state(owner, request, run_id=run_id)

    def runtime_info(self) -> dict[str, Any]:
        return runtime_info(self.config)

    def trace_query(self) -> TraceQueryService:
        return TraceQueryService(self.runtime.trace_store)

    def create_session(
        self,
        session: SessionCreate,
        *,
        identity: RequestIdentity | None = None,
    ) -> SessionRecord:
        runtime = self.runtime
        record = runtime.session_store.create(session)
        del identity
        return record

    def list_sessions(self, user_id: str) -> SessionList:
        sessions = self.runtime.session_store.list_by_user(user_id)
        return SessionList(user_id=user_id, total=len(sessions), sessions=sessions)

    def get_session(self, user_id: str, session_id: str) -> SessionRecord | None:
        return self.runtime.session_store.get(user_id, session_id)

    def delete_session(self, user_id: str, session_id: str) -> bool:
        runtime = self.runtime
        deleted = runtime.session_store.delete(user_id, session_id)
        runtime.embedding_coordinator_store.clear_session(user_id, session_id)
        runtime.visual_semantic_store_pool.clear_session(user_id, session_id)
        runtime.visual_memory_text_index.delete_session(user_id, session_id)
        if deleted:
            clear_conversation_history(user_id, session_id, config=runtime.config)
        return deleted

    def delete_user_runtime_data(self, user_id: str) -> dict[str, int]:
        runtime = self.runtime
        run_history_deleted = (
            runtime.run_history.delete_by_user(user_id)
            if runtime.run_history is not None
            else 0
        )
        trace_deleted = runtime.trace_store.delete_by_user(user_id)
        conversation_sessions_deleted = clear_user_conversation_history(
            user_id, config=runtime.config
        )
        session_records_deleted = runtime.session_store.delete_by_user(user_id)
        runtime.embedding_coordinator_store.clear_user(user_id)
        visual_sessions_deleted = runtime.visual_semantic_store_pool.clear_user(user_id)
        runtime.visual_memory_text_index.delete_user(user_id)
        return {
            "run_history_records": run_history_deleted,
            "trace_events": trace_deleted,
            "conversation_sessions": conversation_sessions_deleted,
            "session_records": session_records_deleted,
            "visual_semantic_sessions": visual_sessions_deleted,
        }
