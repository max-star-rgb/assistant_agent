from types import SimpleNamespace
from datetime import datetime, timezone

from assistant_agent.memory.backends.mem0 import build_mem0_memory_bundle
from assistant_agent.memory.commit_ledger import SQLiteMemoryCommitLedger
from assistant_agent.memory.mem0.models import Mem0IngestionResult, Mem0RecallMemory
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.assistant_graph_state import assistant_turn_state_from_request
from assistant_agent.runtime.requests import UserRequest


class _Client:
    def recall_long_term_memory(self, identity):
        return [
            Mem0RecallMemory(
                memory_id="memory-1",
                text="绝密记忆正文",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]

    def ingest_completed_turn(self, turn):
        return Mem0IngestionResult(accepted=True)


def _state():
    state = assistant_turn_state_from_request(
        UserRequest(user_id="user-1", session_id="session-1", text="绝密用户正文"),
        run_id="run-1",
        trace_id="trace-1",
        agent_id="agent-1",
    )
    state["run"]["status"] = "completed"
    state["final_response"] = {
        "message": "绝密回答正文",
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


def test_memory_nodes_emit_redacted_structural_observability(tmp_path) -> None:
    trace_store = InMemoryTraceStore()
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            invocation_kind="invoke",
            refresh_memory=False,
            trace_store=trace_store,
        )
    )
    bundle = build_mem0_memory_bundle(
        client=_Client(),
        ledger=SQLiteMemoryCommitLedger(tmp_path / "ledger.sqlite3"),
        identity_namespace="tests",
    )

    recalled = bundle.recall_node(_state(), runtime)
    committed = bundle.commit_node(recalled, runtime)

    events = trace_store.list_by_run("run-1")
    assert [event.canonical_event for event in events] == [
        "memory.recall.finished",
        "memory.commit.finished",
    ]
    assert events[0].attributes == {
        "backend_id": "mem0",
        "item_count": 1,
        "char_count": 6,
        "issue_codes": [],
    }
    assert events[1].attributes == {
        "backend_id": "mem0",
        "memory_event_id": committed["memory_commit"]["memory_event_id"],
        "issue_code": None,
    }
    assert all(event.latency_ms is not None for event in events)
    serialized = " ".join(event.model_dump_json() for event in events)
    assert "绝密记忆正文" not in serialized
    assert "绝密用户正文" not in serialized
    assert "绝密回答正文" not in serialized
