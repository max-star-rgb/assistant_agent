"""Governed source-text editing for durable memory."""

from datetime import datetime, timezone

from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.remote import MemoryServiceOperationError
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem


def test_memory_edit_preserves_stable_id_and_records_revision_audit() -> None:
    store = InMemoryStore()
    original = store.save(
        MemoryItem(
            memory_id="editable-memory-1",
            user_id="owner-user",
            project_id="owner-project",
            session_id="source-session",
            scope="project",
            memory_type="preference",
            summary="original-memory-sentinel",
            content={"explicit": True, "summary": "original-memory-sentinel"},
            source="explicit_user_request",
            created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        )
    )
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        user_id="owner-user",
        project_id="owner-project",
        session_id="editing-session",
    )

    updated = manager.update_explicit_for_identity(
        identity,
        memory_id=original.memory_id,
        text="edited-memory-sentinel",
    )

    assert updated is not None
    assert updated.memory_id == original.memory_id
    assert updated.created_at == original.created_at
    assert updated.updated_at is not None
    assert updated.summary == "edited-memory-sentinel"
    assert updated.content["summary"] == "edited-memory-sentinel"
    assert store.get("owner-user", original.memory_id) == updated
    event = manager.list_audit_events_for_identity(
        identity,
        event_type="memory_updated",
    )[0]
    assert event.outcome == "succeeded"
    assert event.counts == {"attempted": 1, "updated": 1, "rejected": 0}


def test_memory_edit_cannot_cross_project_identity() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="isolated-memory-1",
            user_id="owner-user",
            project_id="owner-project",
            session_id="source-session",
            scope="project",
            memory_type="task",
            summary="original-memory-sentinel",
            source="explicit_user_request",
            created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        )
    )
    manager = MemoryManager(store)

    updated = manager.update_explicit_for_identity(
        RequestIdentity.for_user(
            user_id="owner-user",
            project_id="other-project",
            session_id="editing-session",
        ),
        memory_id="isolated-memory-1",
        text="unauthorized-edit-sentinel",
    )

    assert updated is None
    assert store.get("owner-user", "isolated-memory-1").summary == (
        "original-memory-sentinel"
    )


def test_memory_edit_does_not_fallback_to_create_for_external_owner() -> None:
    class ExternalOwnerStore(InMemoryStore):
        external_lifecycle_owner = True

    store = ExternalOwnerStore()
    store.save(
        MemoryItem(
            memory_id="remote-memory-1",
            user_id="owner-user",
            project_id="owner-project",
            session_id="source-session",
            scope="project",
            memory_type="task",
            summary="original-memory-sentinel",
            source="explicit_user_request",
            created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        )
    )
    manager = MemoryManager(store)

    try:
        manager.update_explicit_for_identity(
            RequestIdentity.for_user(
                user_id="owner-user",
                project_id="owner-project",
                session_id="editing-session",
            ),
            memory_id="remote-memory-1",
            text="edited-memory-sentinel",
        )
    except MemoryServiceOperationError as exc:
        assert exc.operation == "update"
        assert exc.recoverable is False
    else:
        raise AssertionError("external owner update must fail closed")

    assert store.get("owner-user", "remote-memory-1").summary == (
        "original-memory-sentinel"
    )
