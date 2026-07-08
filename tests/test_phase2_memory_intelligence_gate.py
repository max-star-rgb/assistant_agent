from datetime import datetime, timezone

import pytest

from assistant_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from assistant_agent.memory.profile import USER_PROFILE_MEMORY_ID
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryQuery
from scripts.run_evals import filter_cases_by_suite, load_cases, run_evals


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_phase2_candidate_memory_is_audit_only_until_explicit_user_memory() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    candidate = manager.save_explicit_for_identity(
        identity,
        text="用户喜欢短句回答",
        source_intent="assistant_candidate",
        source_reason="助手从当前请求推断出稳定表达偏好。",
        future_use="后续回答可以更简短。",
        evidence="用户要求以后回答短一点。",
        created_at=NOW,
    )

    assert candidate.written is False
    assert candidate.source_intent == "assistant_candidate"
    assert store.list_by_user("u1") == []
    events = manager.list_audit_events_for_identity(identity)
    assert events[-1].event_type == "memory_promotion_decided"
    assert events[-1].outcome == "skipped"
    assert events[-1].metadata["source_intent"] == "assistant_candidate"


def test_phase2_profile_memory_supersedes_and_recalls_current_preference() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    old = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我喜欢浅色日系风格",
        content={
            "preference_key": "style",
            "style": "浅色日系",
            "summary": "用户喜欢浅色日系风格。",
        },
        memory_id="style_old",
        created_at=NOW,
    )
    new = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我现在喜欢深色极简风格",
        content={
            "preference_key": "style",
            "style": "深色极简",
            "summary": "用户喜欢深色极简风格。",
        },
        memory_id="style_new",
        created_at=NOW,
    )

    profile = store.get("u1", USER_PROFILE_MEMORY_ID)
    recalled = manager.search(MemoryQuery(user_id="u1", query="风格"))

    old_item = store.get("u1", old.memory_id)
    assert old_item is not None
    assert old_item.content["superseded_by_memory_id"] == new.memory_id
    assert new.content["supersedes_memory_ids"] == [old.memory_id]
    assert profile is not None
    assert profile.content["source_memory_ids"] == [new.memory_id]
    assert profile.content["preferences"] == ["用户喜欢深色极简风格。"]
    assert "style_new" in [item.memory_id for item in recalled.items]
    assert "style_old" not in [item.memory_id for item in recalled.items]


def test_phase2_sensitive_explicit_memory_requires_confirmation_before_durable_write() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    with pytest.raises(MemoryConfirmationRequired) as raised:
        manager.save_explicit_for_identity(
            identity,
            text="记住我的项目路径是 /home/alice/private/project",
        )

    confirmation = raised.value.confirmation
    assert confirmation.status == "pending"
    assert confirmation.summary == "我的项目路径是 [redacted]"
    assert manager.list_for_identity(identity) == []
    assert manager.list_confirmations_for_identity(identity)[0].confirmation_id == confirmation.confirmation_id

    confirmed = manager.confirm_memory_for_identity(identity, confirmation.confirmation_id)
    saved = manager.get_for_identity(identity, confirmed.confirmed_memory_id or "")

    assert confirmed.status == "confirmed"
    assert saved is not None
    assert saved.summary == "我的项目路径是 [redacted]"
    assert saved.content["consent"] == "explicit_confirmation"
    assert saved.content["confirmation_id"] == confirmation.confirmation_id


def test_phase2_memory_eval_suite_remains_green_without_vector_dependencies() -> None:
    cases = filter_cases_by_suite(load_cases(), "memory")

    summary = run_evals(cases, router_mode="rule")

    assert summary["failed"] == 0
    retrieval = summary["memory_retrieval_eval"]
    assert retrieval["total"] >= 10
    assert retrieval["recall_at_k"] == 1.0
    assert retrieval["correct_empty_rate"] == 1.0
    assert retrieval["cross_user_leakage_rate"] == 0.0
    assert retrieval["sensitive_injection_rate"] == 0.0
    assert retrieval["expired_injection_rate"] == 0.0
    assert retrieval["token_budget_compliance"] == 1.0
