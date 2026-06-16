from datetime import datetime, timezone

from multimodal_agent.memory.retrieval import format_memory_context
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.services.trace_store import summarize_graph_state


def test_sensitive_text_is_redacted_in_memory_item_and_context() -> None:
    item = MemoryItem(
        memory_id="m1",
        user_id="u1",
        memory_type="task",
        summary="请记住 Authorization: Bearer secret-token",
        content={"note": "Bearer secret-token"},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    context = format_memory_context([item], max_chars=500)

    assert "secret-token" not in item.summary
    assert "secret-token" not in item.content["note"]
    assert "secret-token" not in context
    assert "[redacted]" in context


def test_trace_state_summary_does_not_include_full_memory_content() -> None:
    class State:
        status = "completed"
        intent = None
        plan = None
        selected_tools = []
        tool_calls = []
        tool_results = []
        errors = []
        memory_context = [
            MemoryItem(
                memory_id="m1",
                user_id="u1",
                memory_type="task",
                summary="secret should not be included",
                content={"note": "secret should not be included"},
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]

    summary = summarize_graph_state({"state": State(), "current_step_index": 0})

    assert "memory_context" not in summary
    assert "secret should not be included" not in str(summary)
