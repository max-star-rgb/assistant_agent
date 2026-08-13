from __future__ import annotations

from assistant_agent.memory.commit_ledger import (
    MemoryCommitRequest,
    SQLiteMemoryCommitLedger,
    memory_commit_input_digest,
    stable_memory_event_id,
)


def _request(*, backend_id: str = "mem0", assistant_text: str = "记住了"):
    input_digest = memory_commit_input_digest(
        user_text="我喜欢简洁回答",
        assistant_text=assistant_text,
        schema_version="completed_turn_v1",
    )
    return MemoryCommitRequest(
        memory_event_id=stable_memory_event_id(
            backend_id=backend_id,
            turn_origin_id="origin-run-1",
            input_digest=input_digest,
            schema_version="completed_turn_v1",
        ),
        backend_id=backend_id,
        turn_origin_id="origin-run-1",
        input_schema_version="completed_turn_v1",
        input_digest=input_digest,
    )


def test_memory_event_id_is_stable_but_bound_to_backend_and_normalized_input() -> None:
    first = _request()
    resumed = _request()
    other_backend = _request(backend_id="langmem")
    other_input = _request(assistant_text="我会保持简洁")

    assert first.memory_event_id == resumed.memory_event_id
    assert first.memory_event_id != other_backend.memory_event_id
    assert first.memory_event_id != other_input.memory_event_id


def test_succeeded_commit_is_deduplicated_without_reinvocation(tmp_path) -> None:
    ledger = SQLiteMemoryCommitLedger(tmp_path / "operations.sqlite3")
    request = _request()

    reservation = ledger.reserve(request)
    assert reservation.disposition == "invoke"
    assert reservation.owner_token
    ledger.succeed(
        request.memory_event_id,
        owner_token=reservation.owner_token,
        outcome_code="accepted",
    )

    duplicate = ledger.reserve(request)
    assert duplicate.disposition == "succeeded"
    assert duplicate.owner_token is None
    assert duplicate.record is not None
    assert duplicate.record.outcome_code == "accepted"


def test_interrupted_invocation_is_persisted_as_outcome_unknown(tmp_path) -> None:
    ledger = SQLiteMemoryCommitLedger(tmp_path / "operations.sqlite3")
    request = _request()
    reservation = ledger.reserve(request)
    assert reservation.owner_token

    ledger.outcome_unknown(
        request.memory_event_id,
        owner_token=reservation.owner_token,
        outcome_code="interrupted",
    )

    duplicate = ledger.reserve(request)
    assert duplicate.disposition == "outcome_unknown"
    assert duplicate.record is not None
    assert duplicate.record.outcome_code == "interrupted"
