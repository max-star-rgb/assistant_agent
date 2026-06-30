import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from multimodal_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID
from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy
from multimodal_agent.memory.sqlite_store import SQLiteMemoryStore
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.memory.write_policy import MemoryWritePolicy, build_task_summary_memory_item
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery
from multimodal_agent.services.memory_audit import MemoryAuditService


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_policy_assigns_expires_at_for_task_memory() -> None:
    item = build_task_summary_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        summary="已完成任务。",
        intent="direct_chat",
        selected_tools=[],
        created_at=NOW,
    )

    assert item is not None
    assert item.expires_at is not None
    assert item.expires_at > item.created_at


def test_policy_keeps_preference_memory_long_lived() -> None:
    policy = MemoryWritePolicy()

    assert policy.expires_at_for("preference", NOW) is None


def test_retrieval_excludes_expired_memory_by_default() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )

    results = MemoryRetrievalStrategy(store).retrieve(MemoryQuery(user_id="u1", query="过期", top_k=5))

    assert results == []


def test_retrieval_can_include_expired_memory_when_requested() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="过期", include_expired=True, top_k=5)
    )

    assert [item.memory_id for item in results] == ["expired"]


def test_memory_export_is_identity_scoped_and_can_include_content() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢浅色。",
            content={"style": "浅色"},
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="m2",
            user_id="u2",
            memory_type="preference",
            summary="其他用户记忆。",
            created_at=NOW,
        )
    )
    service = MemoryAuditService(MemoryManager(store))

    exported = service.export_for_identity(RequestIdentity.for_user(user_id="u1"), include_content=True)

    assert exported.user_id == "u1"
    assert exported.total == 1
    assert exported.items[0].memory_id == "m1"
    assert exported.items[0].content == {"style": "浅色"}


def test_memory_audit_events_and_metrics_cover_lifecycle_operations() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    other_identity = RequestIdentity.for_user(user_id="u2", session_id="s1")

    saved = manager.save_explicit_for_identity(
        identity,
        text="记住我喜欢浅色背景",
        content={"summary": "用户喜欢浅色背景。", "style": "浅色"},
    )
    manager.save_explicit_for_identity(
        other_identity,
        text="记住我喜欢深色背景",
        content={"summary": "用户喜欢深色背景。", "style": "深色"},
    )
    manager.load_context_for_identity(identity, query_text="浅色")
    service.export_for_identity(identity, include_content=False)
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            session_id="s2",
            memory_type="task",
            summary="过期任务",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    service.sweep_expired_for_identity(identity)

    events = service.events_for_identity(identity, limit=20)
    event_types = {event.event_type for event in events.items}
    metrics = service.metrics_for_identity(identity)

    assert saved.memory_id in {event.memory_id for event in events.items if event.memory_id}
    assert {
        "memory_explicit_saved",
        "memory_context_loaded",
        "memory_exported",
        "memory_retention_swept",
        "memory_deleted",
    }.issubset(event_types)
    assert all(event.user_id == "u1" for event in events.items)
    assert metrics.by_event_type["memory_explicit_saved"] == 1
    assert metrics.counters["memory.write.allowed.count"] == 1
    assert metrics.counters["memory.search.count"] == 1
    assert metrics.counters["memory.export.count"] == 1
    assert metrics.counters["memory.ttl.swept.count"] == 1
    assert metrics.counters["memory.delete.soft.count"] == 1


def test_rejected_explicit_memory_emits_audit_event_without_persisting() -> None:
    manager = MemoryManager(InMemoryStore())
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    with pytest.raises(ValueError):
        manager.save_explicit_for_identity(identity, text="记住 sk-secret-token")

    events = service.events_for_identity(identity)
    metrics = service.metrics_for_identity(identity)

    assert events.total == 1
    assert events.items[0].event_type == "memory_explicit_saved"
    assert events.items[0].outcome == "rejected"
    assert events.items[0].metadata["reason"] == "candidate contains secret-like text"
    assert manager.list_for_identity(identity) == []
    assert metrics.counters["memory.write.rejected.count"] == 1


def test_sensitive_explicit_memory_requires_confirmation_before_persisting() -> None:
    manager = MemoryManager(InMemoryStore())
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    with pytest.raises(MemoryConfirmationRequired) as raised:
        manager.save_explicit_for_identity(
            identity,
            text="记住我的项目路径是 /home/alice/private/project",
        )

    confirmation = raised.value.confirmation
    pending = service.confirmations_for_identity(identity)
    metrics_before = service.metrics_for_identity(identity)

    assert confirmation.status == "pending"
    assert confirmation.summary == "我的项目路径是 [redacted]"
    assert confirmation.content_preview["summary"] == "我的项目路径是 [redacted]"
    assert pending.total == 1
    assert pending.items[0].confirmation_id == confirmation.confirmation_id
    assert manager.list_for_identity(identity) == []
    assert metrics_before.counters["memory.write.needs_confirmation.count"] == 1

    result = service.confirm_memory_for_identity(identity, confirmation_id=confirmation.confirmation_id)
    assert result is not None
    saved = manager.get_for_identity(identity, result.memory_id or "")
    metrics_after = service.metrics_for_identity(identity)

    assert result.status == "confirmed"
    assert saved is not None
    assert saved.summary == "我的项目路径是 [redacted]"
    assert "/home/alice" not in saved.summary
    assert saved.content["consent"] == "explicit_confirmation"
    assert saved.content["confirmation_id"] == confirmation.confirmation_id
    assert metrics_after.counters["memory.write.allowed.count"] == 1
    assert metrics_after.counters["memory.write.needs_confirmation.count"] == 1
    assert metrics_after.counters["memory.confirmation.confirmed.count"] == 1
    saved_events = [
        event
        for event in manager.list_audit_events_for_identity(identity)
        if event.event_type == "memory_explicit_saved" and event.memory_id == saved.memory_id
    ]
    assert saved_events[-1].metadata["source_intent"] == "user_confirmed"


def test_sensitive_explicit_memory_confirmation_can_be_rejected() -> None:
    manager = MemoryManager(InMemoryStore())
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    with pytest.raises(MemoryConfirmationRequired) as raised:
        manager.save_explicit_for_identity(
            identity,
            text="记住我的本地文件在 /home/alice/private/report.pdf",
        )

    rejected = service.reject_memory_for_identity(
        identity,
        confirmation_id=raised.value.confirmation.confirmation_id,
    )
    listed = service.confirmations_for_identity(identity, include_resolved=True)
    metrics = service.metrics_for_identity(identity)

    assert rejected is not None
    assert rejected.status == "rejected"
    assert listed.total == 1
    assert listed.items[0].status == "rejected"
    assert manager.list_for_identity(identity) == []
    assert metrics.counters["memory.confirmation.rejected.count"] == 1


def test_session_delete_clears_pending_memory_confirmations() -> None:
    manager = MemoryManager(InMemoryStore())
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    with pytest.raises(MemoryConfirmationRequired):
        manager.save_explicit_for_identity(
            identity,
            text="记住我的临时路径是 /home/alice/private/tmp",
        )

    assert service.confirmations_for_identity(identity).total == 1

    manager.delete_session_for_identity(identity)

    assert service.confirmations_for_identity(identity, include_resolved=True).total == 0


def test_profile_status_and_rebuild_create_missing_profile_from_sources() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="pref",
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户喜欢浅色背景。",
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="scoped",
            user_id="u1",
            tenant_id="tenant_1",
            memory_type="preference",
            summary="租户内偏好不应进入全局画像。",
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    status = service.profile_status_for_identity(identity)
    rebuilt = service.rebuild_profile_for_identity(identity)
    profile = manager.get_for_identity(identity, USER_PROFILE_MEMORY_ID)
    metrics = service.metrics_for_identity(identity)

    assert status.dry_run is True
    assert status.action == "create"
    assert status.repaired is False
    assert status.expected_source_memory_ids == ["pref"]
    assert "profile_missing" in status.issues
    assert rebuilt.repaired is True
    assert rebuilt.action == "create"
    assert profile is not None
    assert profile.content["source_memory_ids"] == ["pref"]
    assert "用户喜欢浅色背景。" in profile.content["preferences"]
    assert metrics.counters["memory.profile.update.count"] == 1


def test_profile_rebuild_updates_stale_profile_and_audit_warnings() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="pref",
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户喜欢黑色包。",
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id=USER_PROFILE_MEMORY_ID,
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户画像：偏好：旧偏好。",
            source="user_profile",
            content={
                "profile_version": 1,
                "preferences": ["旧偏好。"],
                "facts": [],
                "source_memory_ids": ["stale"],
            },
            created_at=NOW,
        )
    )
    service = MemoryAuditService(MemoryManager(store))
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    audit_before = service.audit_for_identity(identity)
    repaired = service.rebuild_profile_for_identity(identity)
    profile = service.get_item_for_identity(identity, memory_id=USER_PROFILE_MEMORY_ID, include_content=True)

    assert "profile_stale_sources" in audit_before.warnings
    assert "profile_missing_sources" in audit_before.warnings
    assert repaired.action == "update"
    assert repaired.repaired is True
    assert profile is not None
    assert profile.content is not None
    assert profile.content["source_memory_ids"] == ["pref"]
    assert profile.content["preferences"] == ["用户喜欢黑色包。"]


def test_profile_status_reports_superseded_preference_conflicts() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    old_item = manager.save_explicit_for_identity(
        identity,
        text="记住我喜欢浅色日系海报",
        content={"preference_key": "style", "style": "浅色日系", "summary": "用户喜欢浅色日系海报。"},
        memory_id="style_old",
        created_at=NOW,
    )
    new_item = manager.save_explicit_for_identity(
        identity,
        text="记住我现在喜欢深色极简海报",
        content={"preference_key": "style", "style": "深色极简", "summary": "用户喜欢深色极简海报。"},
        memory_id="style_new",
        created_at=NOW,
    )

    status = service.rebuild_profile_for_identity(identity, dry_run=True)
    metrics = service.metrics_for_identity(identity)

    assert status.dry_run is True
    assert status.action == "none"
    assert status.superseded_source_memory_ids == [old_item.memory_id]
    assert status.expected_source_memory_ids == [new_item.memory_id]
    assert status.profile_conflicts == [
        {
            "preference_key": "style",
            "active_memory_id": new_item.memory_id,
            "active_memory_ids": [new_item.memory_id],
            "superseded_memory_ids": [old_item.memory_id],
            "unresolved": False,
        }
    ]
    assert "profile_unresolved_conflicts" not in status.issues
    assert metrics.counters["memory.profile.conflict.count"] == 1


def test_profile_rebuild_deletes_orphan_profile() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id=USER_PROFILE_MEMORY_ID,
            user_id="u1",
            session_id="s1",
            memory_type="preference",
            summary="用户画像：偏好：旧偏好。",
            source="user_profile",
            content={
                "profile_version": 1,
                "preferences": ["旧偏好。"],
                "facts": [],
                "source_memory_ids": ["missing"],
            },
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)
    service = MemoryAuditService(manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")

    status = service.profile_status_for_identity(identity)
    repaired = service.rebuild_profile_for_identity(identity)

    assert status.action == "delete"
    assert "profile_orphaned" in status.issues
    assert repaired.repaired is True
    assert manager.get_for_identity(identity, USER_PROFILE_MEMORY_ID) is None


def test_retention_sweep_soft_deletes_expired_visible_items_only() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save(
        MemoryItem(
            memory_id="active",
            user_id="u1",
            memory_type="task",
            summary="有效记忆",
            created_at=NOW,
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save(
        MemoryItem(
            memory_id="other_user_expired",
            user_id="u2",
            memory_type="task",
            summary="其他用户过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    service = MemoryAuditService(MemoryManager(store))

    result = service.sweep_expired_for_identity(RequestIdentity.for_user(user_id="u1"))

    assert result.mode == "soft_delete"
    assert result.scanned == 2
    assert result.expired == 1
    assert result.deleted["memory_items"] == 1
    assert result.memory_ids == ["expired"]
    assert store.get("u1", "expired") is None
    assert store.get("u1", "active") is not None
    assert store.get("u2", "other_user_expired") is not None


def test_retention_sweep_dry_run_does_not_delete() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    service = MemoryAuditService(MemoryManager(store))

    result = service.sweep_expired_for_identity(RequestIdentity.for_user(user_id="u1"), dry_run=True)

    assert result.dry_run is True
    assert result.deleted["memory_items"] == 0
    assert store.get("u1", "expired") is not None


def test_sqlite_retention_sweep_hard_deletes_expired_row(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    store = SQLiteMemoryStore(path)
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    service = MemoryAuditService(MemoryManager(store))

    result = service.sweep_expired_for_identity(
        RequestIdentity.for_user(user_id="u1"),
        hard_delete=True,
    )

    assert result.mode == "hard_delete"
    assert result.deleted["memory_items"] == 1
    assert store.get("u1", "expired") is None
    with closing(sqlite3.connect(path)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_items WHERE user_id = ? AND memory_id = ?",
            ("u1", "expired"),
        ).fetchone()[0]
    assert count == 0
