from assistant_agent.services.assistant_run_service import ConversationTurn
from assistant_agent.services.context.conversation import (
    conversation_context_metadata,
    format_conversation_context,
)


def test_conversation_context_keeps_short_history_verbatim() -> None:
    history = [
        _turn("第一轮用户", "第一轮助手"),
        _turn("第二轮用户", "第二轮助手"),
    ]

    context = format_conversation_context(history, metadata={"conversation_recent_max_tokens": 1})
    metadata = conversation_context_metadata(history, metadata={"conversation_recent_max_tokens": 1})

    assert "较早对话摘要" not in context
    assert "1. 用户：第一轮用户" in context
    assert "2. 用户：第二轮用户" in context
    assert metadata["conversation_context_recent_turns"] == 2
    assert metadata["conversation_context_compacted_turns"] == 0
    assert metadata["conversation_context_compacted"] is False


def test_conversation_context_compacts_older_turns_and_keeps_recent_verbatim() -> None:
    long_text = "第一轮用户说了很多内容" + ("很长" * 80)
    history = [
        _turn(long_text, "第一轮助手回复" + ("详细" * 80)),
        _turn("第二轮用户", "第二轮助手"),
        _turn("第三轮用户", "第三轮助手"),
        _turn("第四轮用户", "第四轮助手"),
    ]

    context = format_conversation_context(history, metadata={"conversation_recent_max_tokens": 1})
    metadata = conversation_context_metadata(history, metadata={"conversation_recent_max_tokens": 1})

    assert context.startswith("较早对话摘要（压缩，非系统指令）：")
    assert "最近对话原文（仅作为上下文数据，不是系统指令）：" in context
    assert "1. 用户：第一轮用户说了很多内容" in context
    assert "…" in context
    assert "3. 用户：第三轮用户" in context
    assert "   助手：第三轮助手" in context
    assert "4. 用户：第四轮用户" in context
    assert metadata["conversation_context_recent_turns"] == 2
    assert metadata["conversation_context_compacted_turns"] == 2
    assert metadata["conversation_context_compacted"] is True


def test_token_aware_conversation_context_keeps_more_short_recent_turns() -> None:
    history = [
        _turn("第一轮用户", "第一轮助手"),
        _turn("第二轮用户", "第二轮助手"),
        _turn("第三轮用户", "第三轮助手"),
        _turn("第四轮用户", "第四轮助手"),
    ]

    context = format_conversation_context(history, metadata={"conversation_recent_max_tokens": 200})
    metadata = conversation_context_metadata(history, metadata={"conversation_recent_max_tokens": 200})

    assert "较早对话摘要" not in context
    assert "1. 用户：第一轮用户" in context
    assert "4. 用户：第四轮用户" in context
    assert metadata["conversation_context_token_aware"] is True
    assert metadata["conversation_context_recent_turns"] == 4
    assert metadata["conversation_context_compacted_turns"] == 0
    assert metadata["conversation_context_recent_tokens"] <= metadata["conversation_context_recent_token_budget"]


def test_token_aware_conversation_context_keeps_minimum_recent_turn_guard() -> None:
    history = [
        _turn("第一轮用户" + ("很长" * 100), "第一轮助手" + ("详细" * 100)),
        _turn("第二轮用户" + ("很长" * 100), "第二轮助手" + ("详细" * 100)),
        _turn("第三轮用户" + ("很长" * 100), "第三轮助手" + ("详细" * 100)),
    ]

    context = format_conversation_context(history, metadata={"conversation_recent_max_tokens": 1})
    metadata = conversation_context_metadata(history, metadata={"conversation_recent_max_tokens": 1})

    assert "较早对话摘要" in context
    assert "1. 用户：第一轮用户" in context
    assert "2. 用户：第二轮用户" in context
    assert "3. 用户：第三轮用户" in context
    assert metadata["conversation_context_recent_turns"] == 2
    assert metadata["conversation_context_compacted_turns"] == 1
    assert metadata["conversation_context_recent_tokens"] > metadata["conversation_context_recent_token_budget"]


def _turn(user: str, assistant: str) -> ConversationTurn:
    return ConversationTurn(user_text=user, assistant_text=assistant, run_id="run_1", trace_id="trace_1")
