from fastapi.testclient import TestClient

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.api import routes_agent
from multimodal_agent.api.app import create_app
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.trace_store import InMemoryTraceStore


def test_trace_query_api_can_query_by_run_id_and_trace_id(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找相似款"},
    )
    run_payload = run_response.json()

    run_summary = client.get(f"/runs/{run_payload['run_id']}").json()
    trace_summary = client.get(f"/traces/{run_payload['trace_id']}").json()

    assert run_summary["run_id"] == run_payload["run_id"]
    assert run_summary["trace_id"] == run_payload["trace_id"]
    assert "assistant" in run_summary["node_path"]
    assert "execute_tool" in run_summary["node_path"]
    assert trace_summary["trace_id"] == run_payload["trace_id"]
    assert trace_summary["run_id"] == run_payload["run_id"]
    assert trace_summary["events"]
    assert run_summary["context"]["budget"]["total_chars"] > 0
    assert trace_summary["context"]["budget"]["total_chars"] > 0
    assert "context_usage_ratio" in trace_summary["context"]["budget"]
    assert "compaction_triggered" in trace_summary["context"]["budget"]
    assert "compactor_type" in trace_summary["context"]
    assert "context_summary_present" in trace_summary["context"]
    assert "memory_promotion_candidates" in trace_summary["context"]
    assert "memory_promotion_written" in trace_summary["context"]
    assert "source_counts" in trace_summary["context"]
    assert "compaction" in trace_summary["context"]
    assert "tool_catalog" in trace_summary["context"]
    assert trace_summary["context"]["tool_catalog"]["total_tool_count"] >= 1
    assert any(
        event["event_type"] == "assistant_decision" and "context" in event["output_summary"]
        for event in trace_summary["events"]
    )


def test_trace_query_api_can_query_tool_calls(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)
    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找相似款"))
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.get(f"/runs/{state.run_id}/tool-calls")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == state.run_id
    assert isinstance(payload["tool_calls"], list)


def test_trace_query_api_returns_404_for_unknown_ids() -> None:
    client = TestClient(create_app())

    assert client.get("/runs/run_missing").status_code == 404
    assert client.get("/traces/trace_missing").status_code == 404
