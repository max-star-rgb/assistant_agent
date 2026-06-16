from datetime import datetime, timezone

from multimodal_agent.memory.retrieval import format_memory_context
from multimodal_agent.schemas.memory import MemoryItem


def test_memory_context_builder_respects_max_chars_and_includes_refs() -> None:
    items = [
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="artifact",
            summary="最近生成过一张白色运动鞋海报。",
            artifact_refs=["mock://image/poster-1"],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        MemoryItem(
            memory_id="m2",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢日系极简浅色背景。" * 10,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]

    context = format_memory_context(items, max_chars=90)

    assert len(context) <= 90
    assert context.startswith("相关历史：")
    assert "mock://image/poster-1" in context
