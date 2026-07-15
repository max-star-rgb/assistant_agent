from assistant_agent.services.assistant_run_service import (
    ConversationTurn,
    InMemoryConversationStore,
)
from assistant_agent.services.trace_conversation import find_trace_conversation


def test_find_trace_conversation_returns_only_matching_bounded_turn() -> None:
    store = InMemoryConversationStore(max_turns=4)
    store.append(
        "user_1",
        "session_1",
        ConversationTurn(
            user_text="previous user text",
            assistant_text="previous assistant text",
            run_id="run_previous",
            trace_id="trace_previous",
        ),
    )
    user_text = "你" * 1001
    assistant_text = "好" * 1002
    store.append(
        "user_1",
        "session_1",
        ConversationTurn(
            user_text=user_text,
            assistant_text=assistant_text,
            run_id="run_target",
            trace_id="trace_target",
        ),
    )

    view = find_trace_conversation(
        store,
        user_id="user_1",
        session_id="session_1",
        trace_id="trace_target",
    )

    assert view is not None
    assert view.schema_version == "trace_conversation_view_v1"
    assert view.trace_id == "trace_target"
    assert view.user.text == user_text[:1000]
    assert view.user.chars == 1001
    assert view.user.truncated is True
    assert view.assistant.text == assistant_text[:1000]
    assert view.assistant.chars == 1002
    assert view.assistant.truncated is True
    assert set(view.model_dump()) == {"schema_version", "trace_id", "user", "assistant"}
    assert "previous" not in str(view.model_dump())


def test_find_trace_conversation_preserves_short_text_and_returns_none_for_unknown_trace() -> None:
    store = InMemoryConversationStore()
    store.append(
        "user_1",
        "session_1",
        ConversationTurn(
            user_text="眼前是什么？",
            assistant_text="眼前是一个杯子。",
            run_id="run_1",
            trace_id="trace_1",
        ),
    )

    view = find_trace_conversation(
        store,
        user_id="user_1",
        session_id="session_1",
        trace_id="trace_1",
    )

    assert view is not None
    assert view.user.model_dump() == {"text": "眼前是什么？", "chars": 6, "truncated": False}
    assert view.assistant.model_dump() == {
        "text": "眼前是一个杯子。",
        "chars": 8,
        "truncated": False,
    }
    assert (
        find_trace_conversation(
            store,
            user_id="user_1",
            session_id="session_1",
            trace_id="trace_missing",
        )
        is None
    )
