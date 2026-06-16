from datetime import datetime, timezone

from multimodal_agent.memory.write_policy import build_explicit_memory_item, build_task_summary_memory_item


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_sensitive_explicit_memory_is_marked_sensitive_and_redacted() -> None:
    item = build_explicit_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        text="记住 Authorization: Bearer secret-token",
        content={},
        created_at=NOW,
    )

    assert item.sensitivity == "sensitive"
    assert "secret-token" not in item.summary
    assert "[redacted]" in item.summary


def test_sensitive_task_summary_is_not_auto_saved_by_default() -> None:
    item = build_task_summary_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        summary="任务完成，Authorization: Bearer secret-token",
        intent="direct_chat",
        selected_tools=[],
        created_at=NOW,
    )

    assert item is None
