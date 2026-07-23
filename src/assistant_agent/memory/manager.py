"""Minimal Mem0 runtime boundary.

The project owns identity binding and the session-start snapshot lifecycle.
Mem0 owns extraction, consolidation, indexing, ranking, and memory updates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem, MemoryPromptSnapshot
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.provider_errors import ProviderSafetyPolicy, sanitize_error_message


_CAPTURE_TEXT_POLICY = ProviderSafetyPolicy(
    max_message_chars=4000,
    max_detail_chars=4000,
    redact_absolute_paths=False,
)


@dataclass(frozen=True)
class PreparedTurnCapture:
    """Immutable input for the post-response Mem0 capture."""

    identity: RequestIdentity
    user_text: str
    assistant_text: str
    occurred_at: datetime
    source_turn: str

    @property
    def ordering_key(self) -> tuple[str, str, str, str]:
        return (
            self.identity.tenant_id or "",
            self.identity.user_id,
            self.identity.project_id or "",
            self.identity.session_id or "",
        )


class MemoryContext(BaseModel):
    """The complete Mem0 result frozen for one session."""

    items: list[MemoryItem] = Field(default_factory=list)
    text: str = ""
    error_codes: list[str] = Field(default_factory=list)
    status: str = "succeeded"


class MemoryManager:
    """Thin runtime adapter over the single Mem0 store."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def load_context_for_request(
        self,
        request: UserRequest,
        *,
        top_k: int | None = None,
        session_initial: bool = False,
    ) -> MemoryContext:
        """Recall Mem0 only from the explicit session-start path."""

        if not session_initial:
            return self.missing_session_snapshot_context()
        try:
            items = self.store.recall(
                RequestIdentity.from_user_request(request),
                top_k=top_k or 5,
            )
        except Exception:
            return MemoryContext(
                status="degraded",
                error_codes=["mem0_recall_failed"],
            )
        return MemoryContext(
            items=items,
            text="\n".join(item.summary for item in items if item.summary),
            status="succeeded",
        )

    def load_into_state(
        self,
        state: Any,
        request: UserRequest,
        **_: Any,
    ) -> MemoryContext:
        """Turns never recall; they only receive an explicit missing snapshot."""

        context = self.missing_session_snapshot_context()
        self.attach_context_to_state(state, context)
        return context

    def attach_context_to_state(self, state: Any, context: MemoryContext) -> None:
        source_ids = [item.memory_id for item in context.items]
        state.memory_context = context.items
        state.request.metadata["memory_prompt_snapshot"] = MemoryPromptSnapshot(
            text=context.text,
            source_ids=source_ids,
        ).model_dump(mode="json")
        state.request.metadata["memory_context_status"] = context.status

    def missing_session_snapshot_context(self) -> MemoryContext:
        return MemoryContext(status="session_snapshot_missing")

    def failed_session_snapshot_context(self) -> MemoryContext:
        return MemoryContext(
            status="session_start_failed",
            error_codes=["memory_session_start_failed"],
        )

    def prepare_completed_turn_capture(
        self,
        state: Any,
    ) -> PreparedTurnCapture | None:
        if not bool(getattr(self.store, "supports_turn_capture", False)):
            return None
        response = getattr(state, "response", None)
        if getattr(state, "status", None) != "completed" or response is None:
            return None
        request = getattr(state, "request", None)
        user_text = sanitize_error_message(
            getattr(request, "text", ""),
            policy=_CAPTURE_TEXT_POLICY,
        )
        assistant_text = sanitize_error_message(
            getattr(response, "message", ""),
            policy=_CAPTURE_TEXT_POLICY,
        )
        if not user_text or not assistant_text:
            return None
        identity = RequestIdentity.from_user_request(request)
        run_id = str(getattr(state, "run_id", "") or "run")
        turn_index = str(request.metadata.get("conversation_turn_index") or "1")
        source_turn = hashlib.sha256(
            f"{run_id}:{turn_index}".encode()
        ).hexdigest()[:24]
        occurred_at = datetime.now(timezone.utc)
        return PreparedTurnCapture(
            identity=identity.model_copy(deep=True),
            user_text=user_text,
            assistant_text=assistant_text,
            occurred_at=occurred_at,
            source_turn=source_turn,
        )

    def capture_prepared_turn(self, prepared: PreparedTurnCapture) -> Any:
        return self.store.capture_turn(
            identity=prepared.identity,
            user_text=prepared.user_text,
            assistant_text=prepared.assistant_text,
            occurred_at=prepared.occurred_at,
            source_turn=prepared.source_turn,
        )
