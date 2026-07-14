from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assistant_agent.schemas.durable_tasks import (
    DurableTaskBundle,
    TaskEvent,
    TaskPlanVersion,
    TaskRecord,
)
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.services.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.services.durable_tasks.store import (
    InMemoryTaskStore,
    TaskAlreadyExists,
    TaskLeaseConflict,
    TaskVersionConflict,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        value = InMemoryTaskStore()
    else:
        value = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    try:
        yield value
    finally:
        value.close()


def test_create_loads_deep_copy_and_rejects_duplicate(store) -> None:
    created = store.create(_bundle(), [_event("task.accepted")])
    loaded = store.load("task_1")

    assert loaded == created
    assert loaded is not created
    loaded.task.objective = "mutated outside store"
    assert store.load("task_1").task.objective == "research"
    with pytest.raises(TaskAlreadyExists):
        store.create(_bundle(), [])


def test_save_is_atomic_and_uses_optimistic_version(store) -> None:
    created = store.create(_bundle(), [_event("task.accepted")])
    changed = created.model_copy(deep=True)
    changed.task.status = "running"

    saved = store.save(
        changed,
        expected_version=created.task.version,
        events=[_event("task.started", status="running")],
    )

    assert saved.task.version == created.task.version + 1
    assert [event.cursor for event in store.list_events("task_1")] == [1, 2]
    with pytest.raises(TaskVersionConflict):
        store.save(changed, expected_version=created.task.version, events=[])
    assert store.load("task_1").task.version == saved.task.version


def test_event_replay_starts_after_cursor_and_is_bounded(store) -> None:
    created = store.create(_bundle(), [_event("task.accepted")])
    changed = created.model_copy(deep=True)
    store.save(
        changed,
        expected_version=created.task.version,
        events=[_event("task.started"), _event("step.started")],
    )

    replay = store.list_events("task_1", after=1, limit=1)

    assert [event.cursor for event in replay] == [2]
    assert replay[0].event_type == "task.started"


def test_only_one_worker_claims_a_task_and_stale_release_is_rejected(store) -> None:
    store.create(_bundle(), [_event("task.accepted")])
    now = datetime.now(timezone.utc)

    first = store.claim_next(worker_id="worker_1", now=now, lease_seconds=30)
    second = store.claim_next(worker_id="worker_2", now=now, lease_seconds=30)

    assert first is not None
    assert second is None
    with pytest.raises(TaskLeaseConflict):
        store.release(
            first.model_copy(update={"lease_token": "stale"}),
            expected_version=first.task_version,
        )
    store.release(first, expected_version=first.task_version)
    assert store.load("task_1").task.lease_token is None


def test_expired_lease_can_be_taken_over(store) -> None:
    store.create(_bundle(), [_event("task.accepted")])
    now = datetime.now(timezone.utc)
    first = store.claim_next(worker_id="worker_1", now=now, lease_seconds=5)

    replacement = store.claim_next(
        worker_id="worker_2",
        now=now + timedelta(seconds=6),
        lease_seconds=5,
    )

    assert first is not None
    assert replacement is not None
    assert replacement.worker_id == "worker_2"
    assert replacement.lease_token != first.lease_token


def test_sqlite_store_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = SQLiteTaskStore(path)
    first.create(_bundle(), [_event("task.accepted")])
    first.close()

    second = SQLiteTaskStore(path)
    try:
        assert second.load("task_1").task.objective == "research"
        assert second.list_events("task_1")[0].cursor == 1
    finally:
        second.close()


def _bundle() -> DurableTaskBundle:
    plan = TaskPlan(
        goal="research",
        steps=[TaskStep(step_id="step_1", action="search", tool_name="web_search")],
    )
    return DurableTaskBundle(
        task=TaskRecord(
            task_id="task_1",
            user_id="u1",
            session_id="s1",
            ingress_run_id="run_1",
            objective="research",
        ),
        plans=[
            TaskPlanVersion(
                task_id="task_1",
                plan_version=1,
                plan=plan,
                revision_reason="initial",
            )
        ],
    )


def _event(event_type: str, *, status: str = "queued") -> TaskEvent:
    return TaskEvent(task_id="task_1", event_type=event_type, status=status)
