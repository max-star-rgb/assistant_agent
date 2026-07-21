"""Explicit, bounded lookup of one trace's persisted conversation turn."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.services.assistant_run_service import ConversationStore


DEFAULT_TRACE_CONVERSATION_CHAR_LIMIT = 1000


class TraceConversationText(BaseModel):
    """One bounded side of a persisted conversation turn."""

    text: str
    chars: int = Field(ge=0)
    truncated: bool = False


class TraceConversationView(BaseModel):
    """Current-turn content joined outside the redacted trace store."""

    schema_version: Literal["trace_conversation_view_v1"] = "trace_conversation_view_v1"
    trace_id: str
    user: TraceConversationText
    assistant: TraceConversationText


@dataclass(frozen=True)
class TraceConversationRecord:
    """One current-turn debug record keyed by trace identity."""

    user_id: str
    session_id: str
    trace_id: str
    user_text: str
    assistant_text: str


class InMemoryTraceConversationStore:
    """Process-local current-turn text lookup for explicit trace debugging.

    This store is intentionally separate from conversation history. Failed turns
    can be inspected locally without becoming future model context.
    """

    def __init__(self, *, max_records: int = 512) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.max_records = max_records
        self._records: list[TraceConversationRecord] = []

    def append(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        record = TraceConversationRecord(
            user_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        self._records = [
            existing
            for existing in self._records
            if not (
                existing.user_id == user_id
                and existing.session_id == session_id
                and existing.trace_id == trace_id
            )
        ]
        self._records = [*self._records, record][-self.max_records :]

    def get(
        self,
        *,
        user_id: str,
        session_id: str,
        trace_id: str,
        limit: int = DEFAULT_TRACE_CONVERSATION_CHAR_LIMIT,
    ) -> TraceConversationView | None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        for record in reversed(self._records):
            if (
                record.user_id == user_id
                and record.session_id == session_id
                and record.trace_id == trace_id
            ):
                return TraceConversationView(
                    trace_id=trace_id,
                    user=_bounded_text(record.user_text, limit=limit),
                    assistant=_bounded_text(record.assistant_text, limit=limit),
                )
        return None


_DEFAULT_TRACE_CONVERSATION_STORE = InMemoryTraceConversationStore()


def get_default_trace_conversation_store() -> InMemoryTraceConversationStore:
    """Return the process-local current-turn trace content store."""

    return _DEFAULT_TRACE_CONVERSATION_STORE


def find_trace_conversation(
    store: ConversationStore,
    *,
    user_id: str,
    session_id: str,
    trace_id: str,
    limit: int = DEFAULT_TRACE_CONVERSATION_CHAR_LIMIT,
) -> TraceConversationView | None:
    """Return only the matching turn, bounded by Unicode character count."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    for turn in reversed(store.get(user_id, session_id)):
        if turn.trace_id != trace_id:
            continue
        return TraceConversationView(
            trace_id=trace_id,
            user=_bounded_text(turn.user_text, limit=limit),
            assistant=_bounded_text(turn.assistant_text, limit=limit),
        )
    return None


def _bounded_text(value: str, *, limit: int) -> TraceConversationText:
    chars = len(value)
    return TraceConversationText(
        text=value[:limit],
        chars=chars,
        truncated=chars > limit,
    )
