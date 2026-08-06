from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.proactive_messages import (
    ProactiveDeliveryAttempt,
    ProactiveMessage,
    ProactiveSessionEventStore,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.media.video.visual_reminder import VisualReminderRegistry
from assistant_agent.tools.registry import ToolRegistry


def _message(message_id: str, *, user_id: str = "user-1") -> ProactiveMessage:
    return ProactiveMessage(
        message_id=message_id,
        user_id=user_id,
        session_id="session-1",
        kind="visual_reminder",
        content=f"content-{message_id}",
        delivery_mode="connection_ephemeral",
        source_run_id="run-1",
        source_trace_id="trace-1",
    )


def test_proactive_message_rejects_empty_identity_and_content() -> None:
    with pytest.raises(ValidationError):
        ProactiveMessage(
            message_id="",
            user_id="user-1",
            session_id="session-1",
            kind="visual_reminder",
            content="message",
            delivery_mode="connection_ephemeral",
        )
    with pytest.raises(ValidationError):
        ProactiveMessage(
            message_id="message-1",
            user_id="user-1",
            session_id="session-1",
            kind="visual_reminder",
            content="   ",
            delivery_mode="connection_ephemeral",
        )


def test_session_event_store_is_bounded_identity_scoped_and_clearable() -> None:
    store = ProactiveSessionEventStore(max_events_per_session=2)
    first = _message("message-1")
    second = _message("message-2")
    third = _message("message-3")
    other_user = _message("message-other", user_id="user-2")

    store.record_sent(first, sent_at_ms=1_000)
    store.record_sent(second, sent_at_ms=2_000)
    store.record_sent(third, sent_at_ms=3_000)
    store.record_sent(other_user, sent_at_ms=4_000)

    assert [
        event.message_id for event in store.recent("user-1", "session-1")
    ] == ["message-2", "message-3"]
    assert store.recent("user-1", "session-1")[0].delivery_scope == (
        "server_transport"
    )
    assert [
        event.message_id for event in store.recent("user-2", "session-1")
    ] == ["message-other"]

    store.clear("user-1", "session-1")

    assert store.recent("user-1", "session-1") == []
    assert len(store.recent("user-2", "session-1")) == 1


def test_runtime_projects_sent_proactive_message_into_next_turn_context() -> None:
    store = ProactiveSessionEventStore()
    store.record_sent(_message("message-1"), sent_at_ms=1_000)
    registry = VisualReminderRegistry(session_event_store=store)

    class RecordingAdapter:
        provider = "scripted"
        model = "scripted"

        def __init__(self) -> None:
            self.requests = []

        def chat(self, request):
            self.requests.append(request)
            return ChatResult(
                provider=self.provider,
                model=self.model,
                finish_reason="stop",
                response_text="ack",
            )

    adapter = RecordingAdapter()
    tools = ToolRegistry()
    tools.seal()
    runtime = AgentGraphRuntime(
        registry=tools,
        config=ProviderConfig(),
        chat_adapter=adapter,
        visual_reminder_registry=registry,
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id="user-1",
                session_id="session-1",
                text="知道了",
            )
        )

        assert state.status == "completed"
        current_user_message = adapter.requests[0].messages[-1]
        assert current_user_message["role"] == "user"
        assert "会话内已发送的主动通知" in current_user_message["content"]
        assert "content-message-1" in current_user_message["content"]
        assert "message-other" not in current_user_message["content"]
        context_event = next(
            event
            for event in runtime.trace_store.list_by_run(state.run_id)
            if event.canonical_event == "context.build.finished"
        )
        report = context_event.output_summary["context_report_v2"]
        assert report["sections"]["proactive_session_events"]["item_count"] == 1
        assert report["sections"]["proactive_session_events"]["source"] == (
            "trusted_runtime.proactive_session_events"
        )
    finally:
        runtime.close()


def test_delivery_attempt_requires_message_identity() -> None:
    with pytest.raises(ValidationError):
        ProactiveDeliveryAttempt(
            message_id="",
            status="sent",
            delivery_scope="server_transport",
        )
