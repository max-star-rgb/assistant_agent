from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from assistant_agent.memory.backends.mem0 import build_mem0_memory_bundle
from assistant_agent.memory.commit_ledger import SQLiteMemoryCommitLedger
from assistant_agent.memory.mem0.models import Mem0IngestionResult, Mem0RecallMemory
from assistant_agent.runtime.assistant_graph_state import (
    AssistantStateCompatibilityError,
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.graph_time_travel import (
    GraphCheckpointSelector,
    GraphForkPatch,
    GraphForkRequest,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import ScriptedChatAdapter, offline_config, sealed_registry


class RecordingMem0Client:
    configured = True

    def __init__(self) -> None:
        self.recall_count = 0
        self.commit_count = 0

    def recall_long_term_memory(self, identity):
        del identity
        self.recall_count += 1
        return [
            Mem0RecallMemory(
                memory_id=f"memory-{self.recall_count}",
                text=f"snapshot-{self.recall_count}",
                relevance=1.0,
                created_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            )
        ]

    def ingest_completed_turn(self, turn):
        del turn
        self.commit_count += 1
        return Mem0IngestionResult(accepted=True)


def _runtime(kind: str, *, refresh_memory: bool = False):
    return SimpleNamespace(
        context=SimpleNamespace(
            invocation_kind=kind,
            refresh_memory=refresh_memory,
        )
    )


def _state():
    state = assistant_turn_state_from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="hello"),
        run_id="origin-run",
        trace_id="trace-1",
        agent_id="agent-1",
    )
    state["run"]["status"] = "completed"
    state["final_response"] = {
        "message": "world",
        "followup_question": None,
        "output_refs": [],
        "citations": [],
    }
    state["response_publish"] = {
        "status": "published",
        "final_fact_id": "fact-1",
        "issue_code": None,
    }
    return state


def _as_invocation(state, kind: str):
    updated = dict(state)
    updated["invocation_kind"] = kind
    if kind in {"replay", "fork"}:
        updated["turn_provenance"] = "time_travel"
    return updated


def test_invoke_then_resume_reuses_snapshot_and_commits_logical_turn_once(
    tmp_path,
) -> None:
    client = RecordingMem0Client()
    bundle = build_mem0_memory_bundle(
        client=client,
        ledger=SQLiteMemoryCommitLedger(tmp_path / "memory.sqlite3"),
        identity_namespace="tests",
    )
    invoked = bundle.recall_node(_state(), _runtime("invoke"))
    committed = bundle.commit_node(invoked, _runtime("invoke"))

    resumed = _as_invocation(committed, "resume")
    resumed = bundle.recall_node(resumed, _runtime("resume"))
    resumed = bundle.commit_node(resumed, _runtime("resume"))

    assert client.recall_count == 1
    assert client.commit_count == 1
    assert (
        resumed["memory_context"]["snapshot_id"]
        == invoked["memory_context"]["snapshot_id"]
    )
    assert (
        resumed["memory_commit"]["memory_event_id"]
        == committed["memory_commit"]["memory_event_id"]
    )


@pytest.mark.parametrize("kind", ["replay", "fork"])
def test_exact_time_travel_inherits_snapshot_and_never_commits(tmp_path, kind) -> None:
    client = RecordingMem0Client()
    bundle = build_mem0_memory_bundle(
        client=client,
        ledger=SQLiteMemoryCommitLedger(tmp_path / f"{kind}.sqlite3"),
        identity_namespace="tests",
    )
    original = bundle.recall_node(_state(), _runtime("invoke"))
    historical = _as_invocation(original, kind)

    recalled = bundle.recall_node(historical, _runtime(kind))
    committed = bundle.commit_node(recalled, _runtime(kind))

    assert client.recall_count == 1
    assert client.commit_count == 0
    assert recalled["memory_context"] == original["memory_context"]
    assert committed["memory_commit"] == {
        "status": "skipped",
        "memory_event_id": None,
        "issue_code": "time_travel_commit_disabled",
    }


def test_refresh_fork_recalls_again_but_still_never_commits(tmp_path) -> None:
    client = RecordingMem0Client()
    bundle = build_mem0_memory_bundle(
        client=client,
        ledger=SQLiteMemoryCommitLedger(tmp_path / "fork.sqlite3"),
        identity_namespace="tests",
    )
    original = bundle.recall_node(_state(), _runtime("invoke"))
    forked = _as_invocation(original, "fork")

    refreshed = bundle.recall_node(forked, _runtime("fork", refresh_memory=True))
    committed = bundle.commit_node(refreshed, _runtime("fork", refresh_memory=True))

    assert client.recall_count == 2
    assert client.commit_count == 0
    assert (
        refreshed["memory_context"]["snapshot_id"]
        != original["memory_context"]["snapshot_id"]
    )
    assert committed["memory_commit"]["issue_code"] == ("time_travel_commit_disabled")


@pytest.mark.parametrize("kind", ["resume", "replay", "fork"])
def test_exact_continuation_without_memory_snapshot_fails_closed(
    tmp_path, kind
) -> None:
    client = RecordingMem0Client()
    bundle = build_mem0_memory_bundle(
        client=client,
        ledger=SQLiteMemoryCommitLedger(tmp_path / f"{kind}.sqlite3"),
        identity_namespace="tests",
    )

    with pytest.raises(AssistantStateCompatibilityError):
        bundle.recall_node(_as_invocation(_state(), kind), _runtime(kind))

    assert client.recall_count == 0


def test_fork_request_exposes_explicit_refresh_memory_opt_in() -> None:
    request = GraphForkRequest(
        selector=GraphCheckpointSelector(history_ref="ghr_" + "a" * 32),
        patch=GraphForkPatch(),
        refresh_memory=True,
    )

    assert request.refresh_memory is True


def test_product_run_does_not_enter_parallel_memory_runtime() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="done",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
    )
    try:
        state = runtime.run_state(
            UserRequest(user_id="user-1", session_id="session-1", text="hello")
        )
    finally:
        runtime.close()

    assert state.status == "completed"
    assert not hasattr(runtime, "long_term_memory_service")
