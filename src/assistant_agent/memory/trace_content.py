"""Local-only content overlay for Mem0 ingestion diagnostics."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Protocol

from pydantic import BaseModel, Field

from assistant_agent.memory.mem0.models import Mem0MemoryChange


class MemoryIngestionTraceContent(BaseModel):
    """Sensitive Mem0 change content kept outside canonical trace events."""

    trace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_turn: str = Field(min_length=1)
    user_text: str | None = None
    assistant_text: str | None = None
    changes: list[Mem0MemoryChange] = Field(default_factory=list)


class MemoryTraceContentStore(Protocol):
    def put(self, content: MemoryIngestionTraceContent) -> None:
        """Store one completed ingestion's sensitive content."""

    def get(
        self,
        *,
        trace_id: str,
        run_id: str,
    ) -> MemoryIngestionTraceContent | None:
        """Return sensitive content for one assistant turn."""


class InMemoryMemoryTraceContentStore:
    """Bounded process-local overlay keyed by assistant trace and run."""

    def __init__(self, *, max_entries: int = 256) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._lock = Lock()
        self._records: OrderedDict[tuple[str, str], MemoryIngestionTraceContent] = (
            OrderedDict()
        )

    def put(self, content: MemoryIngestionTraceContent) -> None:
        key = (content.trace_id, content.run_id)
        with self._lock:
            self._records.pop(key, None)
            self._records[key] = content.model_copy(deep=True)
            while len(self._records) > self.max_entries:
                self._records.popitem(last=False)

    def get(
        self,
        *,
        trace_id: str,
        run_id: str,
    ) -> MemoryIngestionTraceContent | None:
        with self._lock:
            content = self._records.get((trace_id, run_id))
            return content.model_copy(deep=True) if content is not None else None


_DEFAULT_MEMORY_TRACE_CONTENT_STORE = InMemoryMemoryTraceContentStore()


def get_default_memory_trace_content_store() -> InMemoryMemoryTraceContentStore:
    return _DEFAULT_MEMORY_TRACE_CONTENT_STORE
