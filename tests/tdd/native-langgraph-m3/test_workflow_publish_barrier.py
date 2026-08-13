from __future__ import annotations

import pytest

from assistant_agent.workflows.graph_publish import (
    SQLiteWorkflowPublishStore,
    SQLiteWorkflowPublisher,
    WorkflowPublishConflict,
    WorkflowPublishOperation,
)


def _operation(*, result_digest: str = "sha256:" + "a" * 64) -> WorkflowPublishOperation:
    return WorkflowPublishOperation.create(
        workflow_id="wf-publish",
        plan_version=2,
        current_generation_digest="sha256:" + "b" * 64,
        user_id="user-publish",
        agent_id="agent-publish",
        deliverable_artifact_refs=("artifact://report",),
        result_digest=result_digest,
    )


def test_publish_replay_after_prepare_or_commit_is_idempotent(tmp_path):
    path = tmp_path / "publish.sqlite3"
    first = SQLiteWorkflowPublishStore(path)
    operation = _operation()

    prepared = first.prepare(operation)
    assert prepared.status == "prepared"
    assert first.completed_event_count(operation.operation_key) == 0

    # Reconstructing the store models a node crash after prepare and before commit.
    rebuilt = SQLiteWorkflowPublishStore(path)
    assert rebuilt.prepare(operation).status == "prepared"
    publisher = SQLiteWorkflowPublisher(tmp_path / "effects.sqlite3")
    effect = publisher.publish(operation)
    committed = rebuilt.commit(operation, effect_ref=effect)
    assert committed.status == "committed"
    assert rebuilt.completed_event_count(operation.operation_key) == 1

    # A crash after the transaction committed replays the same commit ref and
    # cannot duplicate the product event/outbox fact.
    replayed_effect = SQLiteWorkflowPublisher(
        tmp_path / "effects.sqlite3"
    ).publish(operation)
    replayed = SQLiteWorkflowPublishStore(path).commit(
        operation, effect_ref=replayed_effect
    )
    assert replayed == committed
    assert rebuilt.completed_event_count(operation.operation_key) == 1
    assert publisher.effect_count(operation.operation_key) == 1


def test_publish_same_key_with_different_payload_fails_closed(tmp_path):
    store = SQLiteWorkflowPublishStore(tmp_path / "publish.sqlite3")
    operation = _operation()
    store.prepare(operation)
    with pytest.raises(WorkflowPublishConflict):
        store.prepare(_operation(result_digest="sha256:" + "c" * 64))


def test_publish_effect_then_crash_before_ledger_commit_does_not_repeat_effect(tmp_path):
    ledger_path = tmp_path / "publish.sqlite3"
    effect_path = tmp_path / "effects.sqlite3"
    operation = _operation()
    store = SQLiteWorkflowPublishStore(ledger_path)
    publisher = SQLiteWorkflowPublisher(effect_path)
    store.prepare(operation)
    first_effect = publisher.publish(operation)
    assert publisher.effect_count(operation.operation_key) == 1
    assert store.completed_event_count(operation.operation_key) == 0

    # Process/node crash: neither object survives. Replay calls the publisher
    # with the stable operation key and receives the same effect reference.
    replayed_effect = SQLiteWorkflowPublisher(effect_path).publish(operation)
    assert replayed_effect == first_effect
    assert publisher.effect_count(operation.operation_key) == 1
    committed = SQLiteWorkflowPublishStore(ledger_path).commit(
        operation, effect_ref=replayed_effect
    )
    assert committed.status == "committed"
    assert committed.effect_ref == first_effect.effect_ref
