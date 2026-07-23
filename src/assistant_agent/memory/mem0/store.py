"""Thin Mem0 lifecycle adapter used by the runtime."""

from __future__ import annotations

from datetime import datetime

from assistant_agent.memory.mem0.base import (
    Mem0Adapter,
    bind_mem0_identity,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.mem0 import (
    Mem0ConversationMessage,
    Mem0RecallRequest,
    Mem0TurnCaptureRequest,
    Mem0TurnCaptureResult,
)


class Mem0MemoryStore:
    """Expose only Mem0 recall and completed-turn capture."""

    def __init__(
        self,
        *,
        adapter: Mem0Adapter,
        identity_namespace: str,
    ) -> None:
        self.adapter = adapter
        self.identity_namespace = identity_namespace

    @property
    def supports_turn_capture(self) -> bool:
        return bool(getattr(self.adapter, "configured", True))

    def recall(
        self,
        identity: RequestIdentity,
        *,
        top_k: int = 5,
    ) -> list[MemoryItem]:
        identity = bind_mem0_identity(
            identity,
            namespace=self.identity_namespace,
        )
        recalled = self.adapter.recall(
            Mem0RecallRequest(
                identity=identity,
                top_k=top_k,
            )
        )
        return [
            MemoryItem(
                memory_id=record.engine_id,
                summary=record.text,
                created_at=record.created_at or datetime.now().astimezone(),
                relevance=record.relevance,
            )
            for record in recalled.records
        ]

    def capture_turn(
        self,
        *,
        identity: RequestIdentity,
        user_text: str,
        assistant_text: str,
        occurred_at: datetime,
        source_turn: str,
    ) -> Mem0TurnCaptureResult:
        engine_identity = bind_mem0_identity(
            identity,
            namespace=self.identity_namespace,
        )
        return self.adapter.capture_turn(
            Mem0TurnCaptureRequest(
                identity=engine_identity,
                messages=[
                    Mem0ConversationMessage(
                        role="user",
                        content=user_text,
                    ),
                    Mem0ConversationMessage(
                        role="assistant",
                        content=assistant_text,
                    ),
                ],
                occurred_at=occurred_at,
                source_turn=source_turn,
            )
        )
