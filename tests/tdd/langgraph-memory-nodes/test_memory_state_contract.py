from __future__ import annotations

import json
from datetime import datetime, timezone

from assistant_agent.runtime.assistant_graph_state import (
    ASSISTANT_GRAPH_VERSION,
    ASSISTANT_STATE_SCHEMA_VERSION,
    MemoryCommitState,
    MemoryContext,
    MemoryContextItem,
    ResponsePublishState,
    assistant_turn_state_from_request,
    memory_context_texts,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.requests import UserRequest


def test_memory_state_models_round_trip_as_strict_json() -> None:
    context = MemoryContext(
        backend_id="probe",
        status="ready",
        snapshot_id="snapshot-1",
        items=(
            MemoryContextItem(
                memory_id="memory-1",
                text="用户偏好简洁回答",
                source="probe",
                relevance=0.8,
                updated_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            ),
        ),
        issue_codes=("probe_issue",),
    )
    commit = MemoryCommitState(
        status="succeeded",
        memory_event_id="memory-event-1",
    )
    publish = ResponsePublishState(
        status="published",
        final_fact_id="final-fact-1",
    )

    assert MemoryContext.model_validate_json(context.model_dump_json()) == context
    assert MemoryCommitState.model_validate_json(commit.model_dump_json()) == commit
    assert ResponsePublishState.model_validate_json(publish.model_dump_json()) == publish


def test_new_product_turn_explicitly_resets_memory_channels() -> None:
    state = assistant_turn_state_from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="你好"),
        run_id="run-1",
        trace_id="trace-1",
    )

    assert ASSISTANT_GRAPH_VERSION == "4"
    assert ASSISTANT_STATE_SCHEMA_VERSION == 4
    assert state["memory_context"] is None
    assert state["memory_commit"] == {
        "status": "not_requested",
        "memory_event_id": None,
        "issue_code": None,
    }
    assert state["response_publish"] == {
        "status": "not_requested",
        "final_fact_id": None,
        "issue_code": None,
    }
    assert validate_assistant_turn_state(json.loads(json.dumps(state))) == state


def test_assistant_memory_projection_depends_only_on_ordered_text() -> None:
    first = MemoryContext(
        backend_id="mem0",
        status="ready",
        snapshot_id="snapshot-a",
        items=(
            MemoryContextItem(
                memory_id="mem0-id",
                text="第一条",
                source="mem0",
                relevance=0.99,
            ),
            MemoryContextItem(
                memory_id="mem0-id-2",
                text="第二条",
                source="mem0",
                relevance=0.5,
            ),
        ),
    )
    second = MemoryContext(
        backend_id="langmem",
        status="degraded",
        snapshot_id="snapshot-b",
        items=(
            MemoryContextItem(
                memory_id="different-id",
                text="第一条",
                source="langmem",
                relevance=None,
                updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
            MemoryContextItem(
                memory_id="different-id-2",
                text="第二条",
                source="langmem",
                relevance=0.1,
            ),
        ),
        issue_codes=("backend_degraded",),
    )

    assert memory_context_texts(first) == ("第一条", "第二条")
    assert memory_context_texts(second.model_dump(mode="json")) == (
        "第一条",
        "第二条",
    )


def test_memory_context_rejects_unbounded_or_undeclared_data() -> None:
    payload = {
        "schema_version": 1,
        "backend_id": "probe",
        "status": "ready",
        "snapshot_id": "snapshot-1",
        "items": [
            {
                "memory_id": "memory-1",
                "text": "x" * 4_001,
                "source": "probe",
                "relevance": 0.5,
                "updated_at": None,
                "raw_backend_response": {"secret": "must-not-enter-state"},
            }
        ],
        "issue_codes": [],
    }

    try:
        MemoryContext.model_validate(payload)
    except ValueError:
        pass
    else:  # pragma: no cover - assertion message is clearer than pytest.raises here.
        raise AssertionError("undeclared or unbounded memory data was accepted")
