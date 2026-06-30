from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.api import routes_agent
from multimodal_agent.api.auth import AUTH_MODE_ENV, AUTH_REQUIRE_BOUND_IDENTITY_ENV, AUTH_USER_ID_HEADER
from multimodal_agent.api.app import create_app
from multimodal_agent.memory.manager import MemoryConfirmationRequired
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.services.memory_audit import MemoryAuditService
from multimodal_agent.services.trace_store import InMemoryTraceStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_memory_audit_api_lists_and_gets_user_scoped_items(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "preference", "用户喜欢日系风格。", content={"style": "日系"}))
    store.save(_memory("m2", "u2", "preference", "另一个用户的偏好。"))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    listed = client.get("/memory/users/u1/items")
    item = client.get("/memory/users/u1/items/m1")
    missing = client.get("/memory/users/u1/items/m2")

    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["items"][0]["memory_id"] == "m1"
    assert payload["items"][0]["content"] is None
    assert item.status_code == 200
    assert item.json()["content"] == {"style": "日系"}
    assert missing.status_code == 404


def test_memory_audit_api_rejects_request_identity_when_auth_bound_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "1")
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "preference", "用户喜欢日系风格。", content={"style": "日系"}))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    listed = client.get("/memory/users/u1/items")

    assert listed.status_code == 403
    assert listed.json()["detail"]["code"] == "IDENTITY_NOT_AUTH_BOUND"


def test_memory_audit_api_uses_trusted_header_auth_when_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_MODE_ENV, "trusted_header")
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "true")
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "preference", "用户喜欢日系风格。", content={"style": "日系"}))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    listed = client.get("/memory/users/u1/items", headers={AUTH_USER_ID_HEADER: "u1"})

    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_memory_audit_api_deletes_item_and_session(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "task", "session one", session_id="s1"))
    store.save(_memory("m2", "u1", "task", "session two", session_id="s2"))
    store.save(_memory("m3", "u2", "task", "other user", session_id="s2"))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    deleted_item = client.delete("/memory/users/u1/items/m1")
    deleted_session = client.delete("/memory/users/u1/sessions/s2")

    assert deleted_item.status_code == 200
    assert deleted_item.json()["deleted"]["memory_items"] == 1
    assert deleted_session.status_code == 200
    assert deleted_session.json()["deleted"]["memory_items"] == 1
    assert store.list_by_user("u1") == []
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m3"]


def test_memory_audit_api_report_flags_duplicates_and_profile(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "preference", "用户喜欢浅色背景。"))
    store.save(_memory("m2", "u1", "preference", "用户喜欢浅色背景。"))
    store.save(
        _memory(
            "expired",
            "u1",
            "task",
            "已过期任务。",
            expires_at=NOW - timedelta(days=1),
        )
    )
    store.save(
        MemoryItem(
            memory_id=USER_PROFILE_MEMORY_ID,
            user_id="u1",
            memory_type="preference",
            summary="用户画像：偏好：用户喜欢浅色背景。",
            source="user_profile",
            created_at=NOW,
        )
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    report = client.get("/memory/users/u1/audit")

    assert report.status_code == 200
    payload = report.json()
    assert payload["total"] == 4
    assert payload["by_type"]["preference"] == 3
    assert payload["profile_present"] is True
    assert payload["expired_count"] == 1
    assert set(payload["duplicate_groups"][0]["memory_ids"]) == {"m1", "m2"}
    assert "potential_duplicate_memories" in payload["warnings"]


def test_memory_audit_api_exports_user_memory(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "preference", "用户喜欢日系风格。", content={"style": "日系"}))
    store.save(_memory("m2", "u2", "preference", "其他用户记忆。", content={"style": "其他"}))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    exported = client.get("/memory/users/u1/export")
    redacted = client.get("/memory/users/u1/export", params={"include_content": False})

    assert exported.status_code == 200
    payload = exported.json()
    assert payload["user_id"] == "u1"
    assert payload["total"] == 1
    assert payload["include_content"] is True
    assert payload["items"][0]["memory_id"] == "m1"
    assert payload["items"][0]["content"] == {"style": "日系"}
    assert redacted.status_code == 200
    assert redacted.json()["items"][0]["content"] is None


def test_memory_audit_api_sweeps_expired_memory(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    now = datetime.now(timezone.utc)
    store.save(_memory("expired", "u1", "task", "过期任务", expires_at=now - timedelta(days=1)))
    store.save(_memory("active", "u1", "task", "有效任务", expires_at=now + timedelta(days=1)))
    store.save(_memory("other", "u2", "task", "其他用户过期任务", expires_at=now - timedelta(days=1)))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    dry_run = client.post("/memory/users/u1/retention/sweep", params={"dry_run": True})

    assert dry_run.status_code == 200
    assert dry_run.json()["expired"] == 1
    assert dry_run.json()["deleted"]["memory_items"] == 0
    assert store.get("u1", "expired") is not None
    swept = client.post("/memory/users/u1/retention/sweep")
    assert swept.status_code == 200
    assert swept.json()["deleted"]["memory_items"] == 1
    assert store.get("u1", "expired") is None
    assert store.get("u1", "active") is not None
    assert store.get("u2", "other") is not None


def test_memory_audit_api_lists_events_and_metrics(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    runtime.memory_manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我喜欢日系风格",
        content={"summary": "用户喜欢日系风格。", "style": "日系"},
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    exported = client.get("/memory/users/u1/export", params={"include_content": False})
    events = client.get("/memory/users/u1/events")
    filtered_events = client.get("/memory/users/u1/events", params={"event_type": "memory_exported"})
    metrics = client.get("/memory/users/u1/metrics")

    assert exported.status_code == 200
    assert events.status_code == 200
    event_types = {event["event_type"] for event in events.json()["items"]}
    assert {"memory_explicit_saved", "memory_exported"}.issubset(event_types)
    assert filtered_events.status_code == 200
    assert filtered_events.json()["total"] == 1
    assert filtered_events.json()["items"][0]["event_type"] == "memory_exported"
    assert metrics.status_code == 200
    metric_payload = metrics.json()
    assert metric_payload["by_event_type"]["memory_explicit_saved"] == 1
    assert metric_payload["by_event_type"]["memory_exported"] == 1
    assert metric_payload["counters"]["memory.write.allowed.count"] == 1
    assert metric_payload["counters"]["memory.export.count"] == 1


def test_memory_audit_api_confirms_pending_sensitive_memory(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    with pytest.raises(MemoryConfirmationRequired) as raised:
        runtime.memory_manager.save_explicit_for_identity(
            identity,
            text="记住我的项目路径是 /home/alice/private/project",
        )
    confirmation_id = raised.value.confirmation.confirmation_id
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    listed = client.get("/memory/users/u1/confirmations")
    confirmed = client.post(f"/memory/users/u1/confirmations/{confirmation_id}/confirm")
    item = client.get(f"/memory/users/u1/items/{confirmed.json()['memory_id']}")

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["status"] == "pending"
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert item.status_code == 200
    assert item.json()["summary"] == "我的项目路径是 [redacted]"
    assert item.json()["content"]["consent"] == "explicit_confirmation"


def test_memory_audit_api_checks_and_rebuilds_user_profile(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("pref", "u1", "preference", "用户喜欢日系风格。"))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    status = client.get("/memory/users/u1/profile/status")
    dry_run = client.post("/memory/users/u1/profile/rebuild", params={"dry_run": True})

    assert status.status_code == 200
    assert status.json()["action"] == "create"
    assert status.json()["repaired"] is False
    assert "profile_missing" in status.json()["issues"]
    assert dry_run.status_code == 200
    assert dry_run.json()["dry_run"] is True
    assert runtime.memory_manager.get("u1", USER_PROFILE_MEMORY_ID) is None

    rebuilt = client.post("/memory/users/u1/profile/rebuild")
    profile = client.get("/memory/users/u1/items/user_profile")

    assert rebuilt.status_code == 200
    assert rebuilt.json()["repaired"] is True
    assert profile.status_code == 200
    assert profile.json()["content"]["source_memory_ids"] == ["pref"]


def test_memory_audit_service_uses_request_identity_scope() -> None:
    store = InMemoryStore()
    runtime = AgentGraphRuntime(memory_store=store, trace_store=InMemoryTraceStore())
    store.save(_memory("m1", "u1", "task", "用户一任务", session_id="s1"))
    store.save(_memory("m2", "u1", "task", "用户一另一任务", session_id="s2"))
    store.save(_memory("m3", "u2", "task", "用户二任务", session_id="s2"))
    service = MemoryAuditService(runtime.memory_manager)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s2")

    listed = service.list_items_for_identity(identity)
    deleted = service.delete_session_for_identity(identity)

    assert {item.memory_id for item in listed.items} == {"m1", "m2"}
    assert deleted.deleted["memory_items"] == 1
    assert [item.memory_id for item in store.list_by_user("u1")] == ["m1"]
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m3"]


def _memory(
    memory_id: str,
    user_id: str,
    memory_type: str,
    summary: str,
    *,
    session_id: str = "s1",
    content: dict | None = None,
    expires_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id=session_id,
        memory_type=memory_type,
        summary=summary,
        content=content or {},
        created_at=NOW,
        expires_at=expires_at,
    )
