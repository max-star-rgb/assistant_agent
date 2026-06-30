"""Memory boundary snapshot service."""

from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryQuery
from multimodal_agent.schemas.memory_audit import MemoryAuditItem
from multimodal_agent.schemas.memory_snapshot import (
    ConversationHistorySnapshot,
    ConversationTurnSnapshot,
    MemoryContextSnapshot,
    MemoryLayerSnapshot,
    MemorySnapshot,
    MemoryStorageSnapshot,
)
from multimodal_agent.services.assistant_run_service import ConversationStore
from multimodal_agent.services.memory_audit import MemoryAuditService
from multimodal_agent.services.session_store import SessionStore


class MemorySnapshotService:
    """Build a user-facing view of the active memory boundaries."""

    def __init__(
        self,
        *,
        memory_manager: MemoryManager,
        session_store: SessionStore,
        conversation_store: ConversationStore,
        storage: MemoryStorageSnapshot,
    ) -> None:
        self.memory_manager = memory_manager
        self.session_store = session_store
        self.conversation_store = conversation_store
        self.storage = storage

    def snapshot(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        query: str = "",
        top_k: int = 5,
        max_context_chars: int = 1000,
        include_content: bool = False,
        include_superseded: bool = False,
    ) -> MemorySnapshot:
        """Return a read-only snapshot of session and long-term memory."""

        return self.snapshot_for_identity(
            RequestIdentity.for_user(user_id=user_id, session_id=session_id),
            query=query,
            top_k=top_k,
            max_context_chars=max_context_chars,
            include_content=include_content,
            include_superseded=include_superseded,
        )

    def snapshot_for_identity(
        self,
        identity: RequestIdentity,
        *,
        query: str = "",
        top_k: int = 5,
        max_context_chars: int = 1000,
        include_content: bool = False,
        include_superseded: bool = False,
    ) -> MemorySnapshot:
        """Return a read-only snapshot for an identity."""

        session_id = identity.session_id
        result = self.memory_manager.search_for_identity(
            identity,
            MemoryQuery(
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                query=query,
                allowed_scopes=[
                    scope
                    for scope in identity.allowed_scopes
                    if scope in {"session", "task", "project", "user_profile", "video", "product"}
                ],
                top_k=top_k,
                max_context_chars=max_context_chars,
                include_superseded=include_superseded,
            ),
        )
        context = self.memory_manager.build_context(
            result.items,
            max_chars=max_context_chars,
        )
        return MemorySnapshot(
            user_id=identity.user_id,
            session_id=session_id,
            session=self.session_store.get(identity.user_id, session_id) if session_id else None,
            conversation_history=self._conversation_history(user_id=identity.user_id, session_id=session_id),
            memory_context=MemoryContextSnapshot(
                query=query,
                include_superseded=include_superseded,
                total=len(context.items),
                context_text=context.text,
                summaries=context.summaries,
                artifact_refs=context.artifact_refs,
                blocks=[
                    MemoryLayerSnapshot(
                        layer=block.layer,
                        title=block.title,
                        total=len(block.items),
                        items=[
                            MemoryAuditItem.from_memory(item, include_content=include_content)
                            for item in block.items
                        ],
                    )
                    for block in context.blocks
                ],
            ),
            audit=MemoryAuditService(self.memory_manager).audit_for_identity(identity),
            storage=self.storage,
        )

    def _conversation_history(self, *, user_id: str, session_id: str | None) -> ConversationHistorySnapshot:
        if not session_id:
            return ConversationHistorySnapshot(session_id=None, total=0, turns=[])
        turns = self.conversation_store.get(user_id, session_id)
        return ConversationHistorySnapshot(
            session_id=session_id,
            total=len(turns),
            turns=[
                ConversationTurnSnapshot(
                    user_text=turn.user_text,
                    assistant_text=turn.assistant_text,
                    run_id=turn.run_id,
                    trace_id=turn.trace_id,
                )
                for turn in turns
            ],
        )
