from datetime import datetime, timezone

from assistant_agent.memory.framework.ledger import FrameworkGovernanceLedger
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
        MemoryQuery(user_id="u1", tenant_id="t1", project_id="p1", session_id="s1", query="深色")
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
        MemoryQuery(user_id="u1", tenant_id="t1", project_id="p1", session_id="s1", query="深色")
    )

    assert result.items[0].memory_id == "m1"
    assert result.items[0].tenant_id == "t1"
    assert result.items[0].project_id == "p1"


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
