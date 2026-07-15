from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.memory.write_policy import (
    MemoryWritePolicy,
    build_memory_promotion_candidate,
    build_explicit_memory_item,
    build_memory_item_from_promotion_candidate,
    build_run_summary_promotion_candidate,
    build_task_summary_memory_item,
    promotion_decision_audit_record,
)
from assistant_agent.schemas.memory import MemoryItem


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


def test_explicit_save_policy_returns_first_class_decision() -> None:
    decision = MemoryWritePolicy().evaluate_explicit_save(
        text="记住我喜欢日系极简风格",
        content={"style": "日系极简"},
    )

    assert decision.allowed is True
    assert decision.destination == "user_profile"
    assert decision.reason
    assert decision.require_user_confirmation is False
    assert decision.sensitivity == "low"
    assert decision.ttl_days is None
    assert decision.redacted_payload == {
        "summary": "我喜欢日系极简风格",
        "memory_type": "preference",
        "destination": "user_profile",
        "scope": None,
        "content_keys": ["style"],
        "raw_text_stored": False,
    }
    assert decision.candidate is None


def test_explicit_project_scope_policy_routes_to_project_memory() -> None:
    decision = MemoryWritePolicy().evaluate_explicit_save(
        text="记住这个项目使用浅色日系风格",
        scope="project",
    )

    assert decision.allowed is True
    assert decision.destination == "project_memory"
    assert decision.redacted_payload["scope"] == "project"


def test_explicit_secret_policy_rejects_without_raw_payload() -> None:
    decision = MemoryWritePolicy().evaluate_explicit_save(
        text="记住 Authorization: Bearer secret-token",
    )

    assert decision.allowed is False
    assert decision.destination == "reject"
    assert decision.sensitivity == "secret"
    assert decision.require_user_confirmation is False
    assert "secret-token" not in str(decision.redacted_payload)
    assert "[redacted]" in decision.redacted_payload["summary"]


def test_explicit_memory_requires_real_summary() -> None:
    with pytest.raises(ValueError):
        build_explicit_memory_item(
            memory_id="m1",
            user_id="u1",
            session_id="s1",
            text="记住",
            content={},
            created_at=NOW,
        )


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


def test_memory_promotion_candidate_is_not_written_by_default() -> None:
    candidate = build_memory_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="用户这次完成了一次临时商品搜索。",
        memory_type="task",
        kind="episodic_memory",
        reason="task completed",
    )

    decision = MemoryWritePolicy().evaluate_promotion_candidate(candidate)

    assert decision.allowed is False
    assert decision.destination == "reject"
    assert decision.redacted_payload["proposed_destination"] == "task_checkpoint"
    assert "默认禁止自动 memory write" in decision.reason


def test_explicit_memory_promotion_candidate_is_allowed() -> None:
    candidate = build_memory_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="用户喜欢浅色日系风格。",
        memory_type="preference",
        kind="long_term_memory",
        user_intent_explicit=True,
    )

    decision = MemoryWritePolicy().evaluate_promotion_candidate(candidate)

    assert decision.allowed is True
    assert decision.destination == "user_profile"
    assert "用户明确要求记住" in decision.reason


def test_sensitive_memory_promotion_candidate_is_rejected() -> None:
    candidate = build_memory_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="raw output",
        content={"raw_provider_response": {"api_key": "sk-test"}},
    )

    decision = MemoryWritePolicy(allow_auto_write=True).evaluate_promotion_candidate(candidate)

    assert decision.allowed is False
    assert "raw media/provider payload" in decision.reason


def test_run_summary_promotion_candidate_keeps_only_safe_summary_and_refs() -> None:
    candidate = build_run_summary_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="已生成商品海报。",
        intent="image_generation",
        selected_tools=["image_generation"],
        output_refs=["mock://image/poster-1"],
    )

    assert candidate is not None
    assert candidate.summary == "已生成商品海报。"
    assert candidate.kind == "episodic_memory"
    assert candidate.content == {
        "intent": "image_generation",
        "selected_tools": ["image_generation"],
        "output_refs": ["mock://image/poster-1"],
    }
    assert "run_summary" in candidate.tags


def test_allowed_promotion_candidate_can_be_converted_to_memory_item() -> None:
    policy = MemoryWritePolicy(allow_auto_write=True)
    candidate = build_run_summary_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="已生成商品海报。",
        intent="image_generation",
        selected_tools=["image_generation"],
        output_refs=["mock://image/poster-1"],
        policy=policy,
    )

    assert candidate is not None
    item = build_memory_item_from_promotion_candidate(
        memory_id="m1",
        candidate=candidate,
        policy=policy,
        created_at=NOW,
    )

    assert item is not None
    assert item.memory_type == "task"
    assert item.source == "agent_run_summary_candidate"
    assert item.artifact_refs == ["mock://image/poster-1"]
    assert item.expires_at is not None


def test_promotion_audit_record_does_not_include_candidate_content() -> None:
    candidate = build_memory_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="用户这次完成了一次临时商品搜索。",
        content={"output_refs": ["mock://product/1"]},
    )
    decision = MemoryWritePolicy().evaluate_promotion_candidate(candidate)

    audit = promotion_decision_audit_record(decision)

    assert audit["summary"] == "用户这次完成了一次临时商品搜索。"
    assert audit["destination"] == "reject"
    assert audit["require_user_confirmation"] is False
    assert audit["sensitivity"] == "low"
    assert audit["redacted_payload"]["proposed_destination"] == "task_checkpoint"
    assert "content" not in audit
    assert "output_refs" not in audit


def test_context_summary_candidate_is_rejected() -> None:
    candidate = build_memory_promotion_candidate(
        user_id="u1",
        session_id="s1",
        summary="会话摘要",
        content={"context_summary": {"task_state": "处理中"}},
        source="context_summary",
    )

    decision = MemoryWritePolicy(allow_auto_write=True).evaluate_promotion_candidate(candidate)

    assert decision.allowed is False
    assert "context_summary" in decision.reason


@pytest.mark.parametrize(
    "content",
    [
        {"api_key": "sk-test"},
        {"raw_video": "..."},
        {"provider_response": {"x": 1}},
        {"raw_provider_payload": {"x": 1}},
        {"raw_payload": "..."},
    ],
)
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
