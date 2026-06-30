"""Schemas for memory boundary snapshots."""

from pydantic import BaseModel, Field

from multimodal_agent.schemas.api import PROTOCOL_VERSION
from multimodal_agent.schemas.memory_audit import MemoryAuditItem, MemoryAuditReport
from multimodal_agent.schemas.sessions import SessionRecord


class MemoryStorageSnapshot(BaseModel):
    """Runtime storage boundaries used for a memory snapshot."""

    memory_manager: str = "MemoryManager"
    memory_store: str
    session_store: str
    conversation_store: str
    checkpointer: str = "none"
    langgraph_thread_scope: str = "run_id"


class ConversationTurnSnapshot(BaseModel):
    """Public view of one recent conversation turn."""

    user_text: str
    assistant_text: str
    run_id: str
    trace_id: str


class ConversationHistorySnapshot(BaseModel):
    """Session-scoped short-term conversation context."""

    session_id: str | None = None
    total: int = Field(ge=0)
    turns: list[ConversationTurnSnapshot] = Field(default_factory=list)


class MemoryLayerSnapshot(BaseModel):
    """Prompt-safe long-term memory layer."""

    layer: str
    title: str
    total: int = Field(ge=0)
    items: list[MemoryAuditItem] = Field(default_factory=list)


class MemoryContextSnapshot(BaseModel):
    """Long-term memory context that would be available to the assistant."""

    query: str = ""
    include_superseded: bool = False
    total: int = Field(ge=0)
    context_text: str = ""
    summaries: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    blocks: list[MemoryLayerSnapshot] = Field(default_factory=list)


class MemorySnapshot(BaseModel):
    """One user/session memory boundary snapshot."""

    protocol_version: str = PROTOCOL_VERSION
    user_id: str
    session_id: str | None = None
    session: SessionRecord | None = None
    conversation_history: ConversationHistorySnapshot
    memory_context: MemoryContextSnapshot
    audit: MemoryAuditReport
    storage: MemoryStorageSnapshot
