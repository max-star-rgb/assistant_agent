from datetime import datetime, timezone

from assistant_agent.memory.framework.ledger import FrameworkGovernanceLedger
from assistant_agent.memory.framework.base import bind_engine_identity
from assistant_agent.memory.framework import store as framework_store_module
from assistant_agent.memory.framework.store import FrameworkMemoryStore
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.remote import MemoryServiceOperationError
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.memory_audit import MemoryAuditEvent, MemoryPendingConfirmation
from assistant_agent.schemas.memory_framework import (
    FrameworkHealthResult,
    FrameworkMemoryRecord,
    FrameworkRecallResult,
    FrameworkRetainResult,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.memory_tool import MemorySaveTool


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


class ScriptedEngine:
    name = "mem0"

    def __init__(self) -> None:
        self.fail_retain = False
        self.fail_recall = False
        self.empty_recall_attempts = 0
        self.retained = []
        self.recalled = []
        self.deleted = []

    def health(self):
        return FrameworkHealthResult(status="ok", version="2.0.11")

    def retain(self, request):
        if self.fail_retain:
            raise MemoryServiceOperationError("retain", "sidecar unavailable")
        self.retained.append(request)
        return FrameworkRetainResult(accepted=True, engine_ids=[f"eng-{request.project_memory_id}"])

    def recall(self, request):
        if self.fail_recall:
            raise MemoryServiceOperationError("recall", "sidecar unavailable")
        self.recalled.append(request)
        if self.empty_recall_attempts > 0:
            self.empty_recall_attempts -= 1
            return FrameworkRecallResult(records=[], total=0)
        return FrameworkRecallResult(
            records=[
                FrameworkMemoryRecord(
                    engine_id="eng-m1",
                    project_memory_id="m1",
                    text="用户喜欢深色极简",
                    memory_type="preference",
                    source="explicit_user_request",
                    created_at=NOW,
                    relevance=0.9,
                )
            ],
            total=1,
        )

    def get(self, **kwargs):
        return None

    def list(self, **kwargs):
        return []

    def history(self, **kwargs):
        return []

    def reflect(self, request):
        return {}

    def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return True

    def clear(self, **kwargs):
        return 0

    def export(self, **kwargs):
        return []


class Mem0ScopedScriptedEngine:
    name = "mem0"

    def __init__(self) -> None:
        self.records_by_scope = {}
        self.retained = []
        self.recalled = []
        self.deleted = []
        self.always_empty_recall = False

    def health(self):
        return FrameworkHealthResult(status="ok", version="2.0.11")

    def _key(self, request):
        mem0_identity = request.identity
        scope = request.scope
        if scope == "user_profile":
            return (mem0_identity.user_id, mem0_identity.tenant_tag, "user_filter")
        if scope in {"project", "task", "video", "product"}:
            return (mem0_identity.user_id, mem0_identity.agent_id, mem0_identity.tenant_tag, "agent_filter")
        return (
            mem0_identity.user_id,
            mem0_identity.agent_id,
            mem0_identity.run_id,
            mem0_identity.tenant_tag,
            "run_filter",
        )

    def retain(self, request):
        self.retained.append(request)
        engine_id = f"eng-{request.project_memory_id}"
        record = FrameworkMemoryRecord(
            engine_id=engine_id,
            project_memory_id=request.project_memory_id,
            text=request.text,
            memory_type=request.memory_type,
            source=request.source,
            created_at=request.created_at,
            relevance=0.9,
        )
        self.records_by_scope.setdefault(self._key(request), []).append(record)
        return FrameworkRetainResult(accepted=True, engine_ids=[engine_id])

    def recall(self, request):
        self.recalled.append(request)
        if self.always_empty_recall:
            return FrameworkRecallResult(records=[], total=0)
        records = list(self.records_by_scope.get(self._key(request), []))
        return FrameworkRecallResult(records=records[: request.top_k], total=len(records))

    def get(self, **kwargs):
        return None

    def list(self, **kwargs):
        return []

    def history(self, **kwargs):
        return []

    def reflect(self, request):
        return {}

    def delete(self, **kwargs):
        self.deleted.append(kwargs)
        engine_id = kwargs["engine_id"]
        for key, records in list(self.records_by_scope.items()):
            current = [record for record in records if record.engine_id != engine_id]
            if current:
                self.records_by_scope[key] = current
            else:
                self.records_by_scope.pop(key, None)
        return True

    def clear(self, **kwargs):
        return 0

    def export(self, **kwargs):
        return []


def _item(memory_id: str = "m1", user_id: str = "u1") -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        tenant_id="t1",
        user_id=user_id,
        project_id="p1",
        session_id="s1",
        memory_type="preference",
        summary="用户喜欢深色极简",
        source="explicit_user_request",
        created_at=NOW,
    )


def _scoped_item(
    *,
    memory_id: str,
    user_id: str = "u1",
    tenant_id: str = "t1",
    project_id: str = "p1",
    session_id: str = "s1",
    scope: str = "project",
    memory_type: str = "task",
    summary: str = "项目使用浅色日系风格",
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        scope=scope,
        memory_type=memory_type,
        summary=summary,
        source="explicit_user_request",
        created_at=NOW,
    )


def test_successful_retain_records_only_mapping_and_no_fact_payload(tmp_path) -> None:
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    engine = ScriptedEngine()
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")

    saved = store.save(_item())

    assert saved.memory_id == "m1"
    assert "_framework_retain_status" not in saved.content
    assert engine.retained[0].project_memory_id == "m1"
    mappings = ledger.list_mappings(user_id="u1")
    assert mappings[0].engine_id == "eng-m1"
    assert mappings[0].project_memory_id == "m1"
    assert "用户喜欢深色极简" not in (tmp_path / "ledger.sqlite3").read_bytes().decode("utf-8", "ignore")


def test_failed_retain_is_durable_and_retry_is_idempotent_without_fallback_write(tmp_path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    fallback = InMemoryStore()
    engine = ScriptedEngine()
    engine.fail_retain = True
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(ledger_path),
        identity_namespace="test",
        read_fallback=fallback,
    )

    saved = store.save(_item())

    assert saved.memory_id == "m1"
    assert saved.content["_framework_retain_status"] == "queued"
    assert fallback.list_by_user("u1") == []
    assert store.ledger.pending_outbox_count() == 1

    restarted = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(ledger_path),
        identity_namespace="test",
        read_fallback=fallback,
    )
    engine.fail_retain = False
    report = restarted.retry_outbox()
    second_report = restarted.retry_outbox()

    assert report.succeeded == 1
    assert second_report.attempted == 0
    assert len(engine.retained) == 1
    assert restarted.ledger.pending_outbox_count() == 0


def test_manager_retries_framework_pending_writes_without_store_bypass(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.fail_retain = True
    manager = MemoryManager(
        FrameworkMemoryStore(
            adapter=engine,
            ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
            identity_namespace="test",
        )
    )
    manager.store.save(_item())
    engine.fail_retain = False

    report = manager.retry_pending_writes()

    assert report.attempted == 1
    assert report.succeeded == 1


def test_memory_save_tool_reports_durable_queue_instead_of_claiming_framework_write(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.fail_retain = True
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    manager = MemoryManager(store)

    result = MemorySaveTool().run(
        {
            "content": {"summary": "请记住我喜欢深色极简"},
            "source_intent": "user_explicit",
            "source_reason": "用户明确要求记住",
            "future_use": "未来设计偏好",
            "evidence": "当前用户消息",
        },
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert result.contract.status == "partial"
    assert result.data["status"] == "queued"
    assert result.data["written"] is False
    assert result.data["durable_outbox"] is True


def test_recall_failure_uses_read_only_fallback_and_surfaces_stable_error(tmp_path) -> None:
    fallback = InMemoryStore()
    fallback.save(_item(memory_id="fallback-m1"))
    engine = ScriptedEngine()
    engine.fail_recall = True
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
        read_fallback=fallback,
    )

    result = store.search(MemoryQuery(user_id="u1", tenant_id="t1", project_id="p1", query="深色"))

    assert [item.memory_id for item in result.items] == ["fallback-m1"]
    assert result.errors[0]["code"] == "memory_framework_recall_failed"
    assert result.errors[0]["recoverable"] is True
    assert result.ranking_reason.startswith("framework_degraded_to_v2")


def test_framework_recall_rebinds_result_identity_and_filters_tombstones(tmp_path) -> None:
    engine = ScriptedEngine()
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")
    ledger.record_tombstone(user_id="u1", project_memory_id="m1", engine_id="eng-m1")

    result = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="s1",
            query="深色",
        )
    )

    assert result.items == []
    assert result.total == 0


def test_recall_resolves_engine_id_back_to_project_memory_id(tmp_path) -> None:
    engine = ScriptedEngine()
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")
    store.save(_item())
    engine.recall = lambda request: FrameworkRecallResult(
        records=[
            FrameworkMemoryRecord(
                engine_id="eng-m1",
                text="用户喜欢深色极简",
                memory_type="preference",
            )
        ],
        total=1,
    )

    result = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="s1",
            query="深色",
        )
    )

    assert result.items[0].memory_id == "m1"
    assert result.items[0].tenant_id == "t1"
    assert result.items[0].project_id == "p1"


def test_project_memory_is_recalled_across_sessions_for_same_user_and_project(tmp_path) -> None:
    engine = Mem0ScopedScriptedEngine()
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_scoped_item(memory_id="project-memory", session_id="session-a"))

    result = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="session-b",
            query="日系风格",
            allowed_scopes=["project"],
            top_k=5,
        )
    )

    assert [item.memory_id for item in result.items] == ["project-memory"]
    assert result.items[0].session_id == "session-a"
    assert engine.retained[0].scope == "project"
    assert engine.recalled[0].scope == "project"


def test_session_memory_is_only_recalled_in_current_session(tmp_path) -> None:
    engine = Mem0ScopedScriptedEngine()
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(
        _scoped_item(
            memory_id="session-memory",
            session_id="session-a",
            scope="session",
            memory_type="conversation",
            summary="当前会话临时结论",
        )
    )

    same_session = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="session-a",
            query="临时结论",
            allowed_scopes=["session"],
            top_k=5,
        )
    )
    other_session = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="session-b",
            query="临时结论",
            allowed_scopes=["session"],
            top_k=5,
        )
    )

    assert [item.memory_id for item in same_session.items] == ["session-memory"]
    assert other_session.items == []


def test_user_profile_memory_crosses_project_and_session_but_not_user_or_tenant(tmp_path) -> None:
    engine = Mem0ScopedScriptedEngine()
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(
        _scoped_item(
            memory_id="profile-memory",
            project_id="project-a",
            session_id="session-a",
            scope="user_profile",
            memory_type="preference",
            summary="用户喜欢短句回答",
        )
    )

    same_user_other_project = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="project-b",
            session_id="session-b",
            query="回答偏好",
            allowed_scopes=["user_profile"],
            top_k=5,
        )
    )
    other_user = store.search(
        MemoryQuery(
            user_id="u2",
            tenant_id="t1",
            project_id="project-b",
            session_id="session-b",
            query="回答偏好",
            allowed_scopes=["user_profile"],
            top_k=5,
        )
    )
    other_tenant = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t2",
            project_id="project-b",
            session_id="session-b",
            query="回答偏好",
            allowed_scopes=["user_profile"],
            top_k=5,
        )
    )

    assert [item.memory_id for item in same_user_other_project.items] == ["profile-memory"]
    assert other_user.items == []
    assert other_tenant.items == []


def test_project_memory_does_not_cross_projects_by_default(tmp_path) -> None:
    engine = Mem0ScopedScriptedEngine()
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_scoped_item(memory_id="project-a-memory", project_id="project-a"))

    result = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="project-b",
            session_id="session-b",
            query="日系风格",
            allowed_scopes=["project"],
            top_k=5,
        )
    )

    assert result.items == []


def test_delete_project_memory_uses_mapping_across_sessions(tmp_path) -> None:
    engine = Mem0ScopedScriptedEngine()
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_scoped_item(memory_id="project-memory", session_id="session-a"))

    deleted = store.delete_for_identity(
        RequestIdentity.for_user(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="session-b",
        ),
        "project-memory",
    )

    assert deleted is True
    assert engine.deleted[0]["engine_id"] == "eng-project-memory"


def test_recent_retain_fallback_is_scope_aware(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(framework_store_module, "_MEM0_RECALL_CONSISTENCY_TIMEOUT_SECONDS", 0.0)
    engine = Mem0ScopedScriptedEngine()
    engine.always_empty_recall = True
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_scoped_item(memory_id="project-memory", session_id="session-a"))
    store.save(
        _scoped_item(
            memory_id="session-memory",
            session_id="session-a",
            scope="session",
            memory_type="conversation",
            summary="当前会话临时结论",
        )
    )

    same_project = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="session-b",
            query="日系风格",
            allowed_scopes=["project"],
            top_k=5,
        )
    )
    other_project = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p2",
            session_id="session-b",
            query="日系风格",
            allowed_scopes=["project"],
            top_k=5,
        )
    )
    other_session = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="session-b",
            query="临时结论",
            allowed_scopes=["session"],
            top_k=5,
        )
    )

    assert [item.memory_id for item in same_project.items] == ["project-memory"]
    assert other_project.items == []
    assert other_session.items == []


def test_mem0_recall_retries_recent_successful_retain_when_engine_is_eventually_consistent(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.empty_recall_attempts = 1
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_item())

    result = store.search(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="s1",
            query="深色",
            allowed_scopes=["user_profile"],
        )
    )

    assert [item.memory_id for item in result.items] == ["m1"]
    assert len(engine.recalled) == 2


def test_mem0_recall_uses_transient_recent_retain_when_engine_accepts_but_never_indexes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(framework_store_module, "_MEM0_RECALL_CONSISTENCY_TIMEOUT_SECONDS", 0.0)
    engine = ScriptedEngine()
    engine.empty_recall_attempts = 10
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_item())

    result = store.search(
        MemoryQuery(user_id="u1", tenant_id="t1", project_id="p1", session_id="s1", query="深色")
    )

    assert [item.memory_id for item in result.items] == ["m1"]
    assert result.ranking_reason == "framework_mem0_recent_retain_consistency"
    assert "用户喜欢深色极简" not in (tmp_path / "ledger.sqlite3").read_bytes().decode("utf-8", "ignore")


def test_delete_removes_transient_recent_retain_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(framework_store_module, "_MEM0_RECALL_CONSISTENCY_TIMEOUT_SECONDS", 0.0)
    engine = ScriptedEngine()
    engine.empty_recall_attempts = 10
    engine.retain = lambda request: FrameworkRetainResult(accepted=True, engine_ids=["uuid-mem0-engine-id"])
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    store.save(_item())

    assert store.delete("u1", "m1") is True
    result = store.search(
        MemoryQuery(user_id="u1", tenant_id="t1", project_id="p1", session_id="s1", query="深色")
    )

    assert result.items == []


def test_delete_uses_mapping_and_keeps_tombstone_when_sidecar_fails(tmp_path) -> None:
    engine = ScriptedEngine()
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")
    store.save(_item())

    def fail_delete(**kwargs):
        raise MemoryServiceOperationError("delete", "sidecar unavailable")

    engine.delete = fail_delete
    assert store.delete("u1", "m1") is True

    assert ledger.is_tombstoned(user_id="u1", project_memory_id="m1", engine_id="eng-m1")
    assert ledger.pending_outbox_count() == 1


def test_project_scoped_delete_tombstones_all_mappings_with_one_engine_call(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.project_scoped_delete = True
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")
    store.save(_item())
    first = ledger.list_mappings(user_id="u1")[0]
    ledger.record_mapping(
        user_id="u1",
        tenant_id="t1",
        project_id="p1",
        session_id="s1",
        project_memory_id="m1",
        engine_id="eng-m1-second-fact",
        engine_name=engine.name,
        identity=first.identity,
    )

    assert store.delete("u1", "m1") is True

    assert len(engine.deleted) == 1
    assert ledger.is_tombstoned(user_id="u1", project_memory_id="m1", engine_id="eng-m1")
    assert ledger.is_tombstoned(
        user_id="u1", project_memory_id="m1", engine_id="eng-m1-second-fact"
    )


def test_delete_cancels_pending_retain_before_sidecar_recovery(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.fail_retain = True
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")
    store.save(_item())

    assert store.delete("u1", "m1") is True
    engine.fail_retain = False
    report = store.retry_outbox()

    assert report.attempted == 0
    assert engine.retained == []
    assert ledger.is_tombstoned(user_id="u1", project_memory_id="m1", engine_id="pending")


def test_manager_pending_delete_keeps_tenant_and_project_isolation(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.fail_retain = True
    ledger = FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3")
    store = FrameworkMemoryStore(adapter=engine, ledger=ledger, identity_namespace="test")
    manager = MemoryManager(store)
    item = store.save(_item())
    owner = RequestIdentity.for_user(user_id="u1", tenant_id="t1", project_id="p1", session_id="s2")
    attacker = RequestIdentity.for_user(user_id="u1", tenant_id="t2", project_id="p1", session_id="s2")

    assert manager.delete_for_identity(attacker, item.memory_id) is False
    assert manager.delete_for_identity(owner, item.memory_id) is True
    assert ledger.pending_outbox_count() == 0


def test_list_restores_governance_scope_and_manager_filters_other_project(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.get = lambda **kwargs: {
        "id": kwargs["engine_id"],
        "memory": "project private preference",
        "metadata": {"memory_type": "preference", "source": "explicit_user_request"},
        "created_at": NOW.isoformat(),
    }
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    manager = MemoryManager(store)
    store.save(_item())

    owner_items = manager.list_for_identity(
        RequestIdentity.for_user(user_id="u1", tenant_id="t1", project_id="p1")
    )
    other_project_items = manager.list_for_identity(
        RequestIdentity.for_user(user_id="u1", tenant_id="t1", project_id="p2")
    )

    assert owner_items[0].tenant_id == "t1"
    assert owner_items[0].project_id == "p1"
    assert owner_items[0].session_id == "s1"
    assert other_project_items == []


def test_confirmation_and_audit_are_durable_governance_state(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    store = FrameworkMemoryStore(
        adapter=ScriptedEngine(),
        ledger=FrameworkGovernanceLedger(path),
        identity_namespace="test",
    )
    confirmation = MemoryPendingConfirmation(
        confirmation_id="c1",
        user_id="u1",
        tenant_id="t1",
        project_id="p1",
        session_id="s1",
        memory_type="task",
        destination="long_term_memory",
        sensitivity="sensitive",
        reason="needs confirmation",
        summary="redacted summary",
        created_at=NOW,
    )
    event = MemoryAuditEvent(
        event_id="e1",
        event_type="memory_confirmation_created",
        user_id="u1",
        tenant_id="t1",
        project_id="p1",
        occurred_at=NOW,
        summary="confirmation created",
    )

    store.save_confirmation(confirmation)
    store.save_audit_event(event)
    restarted = FrameworkMemoryStore(
        adapter=ScriptedEngine(),
        ledger=FrameworkGovernanceLedger(path),
        identity_namespace="test",
    )

    assert restarted.get_confirmation("u1", "c1") == confirmation
    assert restarted.list_confirmations(user_id="u1", tenant_id="t1", project_id="p1") == [confirmation]
    assert restarted.list_audit_events(user_id="u1", tenant_id="t1", project_id="p1") == [event]

    restarted.clear_user("u1")
    assert restarted.list_confirmations(user_id="u1", tenant_id="t1", project_id="p1") == []


def test_manager_audits_framework_recall_degradation(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.fail_recall = True
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        user_id="u1", tenant_id="t1", project_id="p1", session_id="s1"
    )

    result = manager.search_for_identity(
        identity,
        MemoryQuery(user_id="model-controlled", query="上次偏好"),
    )

    assert result.errors[0]["code"] == "memory_framework_recall_failed"
    events = manager.list_audit_events_for_identity(identity, event_type="memory_framework_degraded")
    assert len(events) == 1
    assert events[0].metadata["error_code"] == "memory_framework_recall_failed"
    assert "sidecar unavailable" not in events[0].model_dump_json()


def test_manager_context_recall_binds_trusted_session_to_framework_run_scope(tmp_path) -> None:
    engine = ScriptedEngine()
    manager = MemoryManager(
        FrameworkMemoryStore(
            adapter=engine,
            ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
            identity_namespace="test",
        )
    )
    identity = RequestIdentity.for_user(
        user_id="u1", tenant_id="t1", project_id="p1", session_id="trusted-session"
    )

    manager.load_context_for_identity(identity, query_text="深色")

    expected = bind_engine_identity(identity, namespace="test")
    assert engine.recalled[0].identity.run_id == expected.run_id


def test_manager_delegates_dedupe_conflict_and_profile_algorithms_to_framework(tmp_path) -> None:
    engine = ScriptedEngine()
    engine.get = lambda **kwargs: {
        "id": kwargs["engine_id"],
        "memory": "用户喜欢深色极简",
        "metadata": {
            "project_memory_id": kwargs["engine_id"].removeprefix("eng-"),
            "memory_type": "preference",
            "source": "explicit_user_request",
        },
        "created_at": NOW.isoformat(),
    }
    store = FrameworkMemoryStore(
        adapter=engine,
        ledger=FrameworkGovernanceLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="test",
    )
    manager = MemoryManager(store)
    first = _item(memory_id="m1").model_copy(update={"tenant_id": None, "project_id": None})
    second = _item(memory_id="m2").model_copy(update={"tenant_id": None, "project_id": None})

    manager._merge_or_save(first)
    manager._merge_or_save(second)
    profile = manager._upsert_user_profile(second)

    assert [request.project_memory_id for request in engine.retained] == ["m1", "m2"]
    assert profile is None
    assert all(request.project_memory_id != "user_profile" for request in engine.retained)
