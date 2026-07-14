"""Explicit, bounded lookup of one trace's persisted conversation turn."""

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
