from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from multimodal_agent.memory.write_policy import (
    MemoryWritePolicy,
    build_explicit_memory_item,
    build_task_summary_memory_item,
)
from multimodal_agent.schemas.memory import MemoryItem


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_explicit_remember_writes_preference_memory_without_raw_text() -> None:
    item = build_explicit_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        text="记住我喜欢日系极简风格",
        content={"style": "日系极简"},
        created_at=NOW,
    )

    assert item.memory_type == "preference"
    assert item.summary == "我喜欢日系极简风格"
    assert item.content == {"explicit": True, "style": "日系极简"}
    assert "text" not in item.content


def test_explicit_remember_writes_liked_character_as_preference() -> None:
    item = build_explicit_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        text="记住我爱玉桂狗",
        created_at=NOW,
    )

    assert item.memory_type == "preference"
    assert item.summary == "我爱玉桂狗"


def test_task_summary_auto_save_keeps_artifact_output_refs_only() -> None:
    item = build_task_summary_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        summary="已生成商品海报。",
        intent="image_generation",
        selected_tools=["image_generation"],
        output_refs=["mock://image/poster-1"],
        created_at=NOW,
    )

    assert item is not None
    assert item.memory_type == "task"
    assert item.artifact_refs == ["mock://image/poster-1"]
    assert item.content["output_refs"] == ["mock://image/poster-1"]
    assert "query" not in item.content


def test_write_policy_can_disable_auto_task_summary() -> None:
    item = build_task_summary_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        summary="已完成任务。",
        intent="direct_chat",
        selected_tools=[],
        policy=MemoryWritePolicy(auto_save_task_summary=False),
        created_at=NOW,
    )

    assert item is None


@pytest.mark.parametrize("content", [{"api_key": "sk-test"}, {"raw_video": "..."}, {"provider_response": {"x": 1}}])
def test_memory_write_rejects_secrets_and_raw_payloads(content: dict) -> None:
    with pytest.raises(ValidationError):
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="task",
            summary="unsafe",
            content=content,
            created_at=NOW,
        )
