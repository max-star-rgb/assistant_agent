from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from assistant_agent.memory.backends.mem0 import build_mem0_memory_bundle
from assistant_agent.memory.commit_ledger import SQLiteMemoryCommitLedger
from assistant_agent.memory.mem0.models import Mem0IngestionResult, Mem0RecallMemory
from assistant_agent.runtime.assistant_graph_state import (
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.requests import UserRequest


class FakeMem0Client:
    configured = True

    def __init__(self) -> None:
        self.memories: list[Mem0RecallMemory] = []
        self.recall_error: Exception | None = None
        self.ingest_error: BaseException | None = None
        self.ingest_result = Mem0IngestionResult(accepted=True)
        self.recall_identities = []
        self.turns = []
        self.closed = False

    def recall_long_term_memory(self, identity):
        self.recall_identities.append(identity)
        if self.recall_error is not None:
            raise self.recall_error
        return list(self.memories)

    def ingest_completed_turn(self, turn):
        self.turns.append(turn)
        if self.ingest_error is not None:
            raise self.ingest_error
        return self.ingest_result

    def close(self) -> None:
        self.closed = True


def _completed_state():
    state = assistant_turn_state_from_request(
        UserRequest(
            user_id="trusted-user",
            session_id="trusted-session",
            text="我喜欢简洁回答",
        ),
        run_id="run-1",
        trace_id="trace-1",
        agent_id="agent-1",
    )
    state["run"]["status"] = "completed"
    state["final_response"] = {
        "message": "好的，我会保持简洁。",
        "followup_question": None,
        "output_refs": [],
        "citations": [],
    }
    state["response_publish"] = {
        "status": "published",
        "final_fact_id": "final-1",
        "issue_code": None,
    }
    return state


def _bundle(tmp_path, client: FakeMem0Client):
    return build_mem0_memory_bundle(
        client=client,
        ledger=SQLiteMemoryCommitLedger(tmp_path / "memory.sqlite3"),
        identity_namespace="tests",
    )


def test_mem0_recall_normalizes_orders_and_bounds_snapshot(tmp_path) -> None:
    client = FakeMem0Client()
    client.memories = [
        Mem0RecallMemory(
            memory_id="low",
            text="低相关",
            relevance=0.2,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        Mem0RecallMemory(
            memory_id="high",
            text="高相关",
            relevance=0.9,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
    ]

    recalled = _bundle(tmp_path, client).recall_node(_completed_state(), None)

    assert recalled["memory_context"]["status"] == "ready"
    assert [item["text"] for item in recalled["memory_context"]["items"]] == [
        "高相关",
        "低相关",
    ]
    assert all(item["source"] == "mem0" for item in recalled["memory_context"]["items"])
    identity = client.recall_identities[0]
    assert identity.user_id.startswith("usr_")
    assert "trusted-user" not in identity.user_id


def test_mem0_empty_and_degraded_recall_are_explicit(tmp_path) -> None:
    client = FakeMem0Client()
    bundle = _bundle(tmp_path, client)

    empty = bundle.recall_node(_completed_state(), None)
    assert empty["memory_context"]["status"] == "empty"

    client.recall_error = RuntimeError("secret backend response")
    degraded = bundle.recall_node(_completed_state(), None)
    assert degraded["memory_context"]["status"] == "degraded"
    assert degraded["memory_context"]["issue_codes"] == ["mem0_recall_failed"]
    assert "secret backend response" not in str(degraded)


def test_mem0_commit_calls_client_once_and_deduplicates_resume(tmp_path) -> None:
    client = FakeMem0Client()
    bundle = _bundle(tmp_path, client)
    state = _completed_state()

    first = bundle.commit_node(state, None)
    resumed = dict(state)
    resumed["invocation_kind"] = "resume"
    duplicate = bundle.commit_node(resumed, None)

    assert first["memory_commit"]["status"] == "succeeded"
    assert duplicate["memory_commit"] == first["memory_commit"]
    assert len(client.turns) == 1
    assert client.turns[0].user_text == "我喜欢简洁回答"
    assert client.turns[0].assistant_text == "好的，我会保持简洁。"
    assert client.turns[0].source_turn == first["memory_commit"]["memory_event_id"]


def test_mem0_commit_failure_and_timeout_only_update_redacted_outcome(tmp_path) -> None:
    failed_client = FakeMem0Client()
    failed_client.ingest_result = Mem0IngestionResult(
        accepted=False,
        errors=[{"code": "raw-third-party-detail", "message": "secret"}],
    )
    failed = _bundle(tmp_path / "failed", failed_client).commit_node(
        _completed_state(), None
    )
    assert failed["memory_commit"]["status"] == "failed"
    assert failed["memory_commit"]["issue_code"] == "mem0_commit_failed"
    assert "secret" not in str(failed)

    timeout_client = FakeMem0Client()
    timeout_client.ingest_error = TimeoutError("private timeout detail")
    timed_out = _bundle(tmp_path / "timed-out", timeout_client).commit_node(
        _completed_state(), None
    )
    assert timed_out["memory_commit"]["status"] == "timed_out"
    assert timed_out["memory_commit"]["issue_code"] == "mem0_commit_timed_out"
    assert "private timeout detail" not in str(timed_out)


def test_mem0_bundle_exposes_only_resource_close_callback(tmp_path) -> None:
    client = FakeMem0Client()
    bundle = _bundle(tmp_path, client)

    assert bundle.store is None
    assert bundle.aclose is not None
    asyncio.run(bundle.aclose())
    assert client.closed is True
