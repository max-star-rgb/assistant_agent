from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent


def test_trace_query_api_can_query_by_run_id_and_trace_id(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "帮我找相似款",
            "metadata": {"context_budget_estimate_tokens": True, "context_budget_max_tokens": 1000},
        },
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
    assert trace_summary["context"]["context_schema_version"] == "context_observability_v1"
    assert "context_usage_ratio" in trace_summary["context"]["budget"]
    assert "compaction_triggered" in trace_summary["context"]["budget"]
    assert trace_summary["context"]["budget"]["token_budget_source"] == "estimated"
    assert trace_summary["context"]["budget"]["total_tokens"] > 0
    assert trace_summary["context"]["budget"]["max_tokens"] == 1000
    assert "compactor_type" in trace_summary["context"]
    assert "context_summary_present" in trace_summary["context"]
    assert "memory_promotion_candidates" in trace_summary["context"]
    assert "memory_promotion_written" in trace_summary["context"]
    assert trace_summary["context"]["memory_promotion_candidates"] == 1
    assert trace_summary["context"]["memory_promotion_written"] == 0
    assert trace_summary["context"]["memory_promotion_rejected"] == 1
    assert "content" not in trace_summary["context"]["memory_promotion_candidate_audit"][0]
    assert "source_counts" in trace_summary["context"]
    assert "compaction" in trace_summary["context"]
    assert "pruned_payload_keys" in trace_summary["context"]["compaction"]
    assert "command_outputs_truncated" in trace_summary["context"]["compaction"]
    assert "tool_catalog" in trace_summary["context"]
    assert trace_summary["context"]["tool_catalog"]["total_tool_count"] >= 1
    assert any(
        event["event_type"] == "assistant_decision" and "context" in event["output_summary"]
        for event in trace_summary["events"]
    )

    run_context = client.get(f"/runs/{run_payload['run_id']}/context").json()
    trace_context = client.get(f"/traces/{run_payload['trace_id']}/context").json()

    assert run_context["run_id"] == run_payload["run_id"]
    assert run_context["trace_id"] == run_payload["trace_id"]
    assert run_context["context_report_v1"]["schema_version"] == "context_report_v1"
    assert trace_context["trace_id"] == run_payload["trace_id"]
    assert trace_context["context_report_v1"]["sections"]["request"]["chars"] > 0
    assert "system_prompt" in trace_context["context_report_v1"]["sections"]
    assert "tool_schema" in trace_context["context_report_v1"]["sections"]
    assert isinstance(trace_context["context_report_v1"]["selected_tool_names"], list)


def test_trace_query_context_api_degrades_legacy_context_summary(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_legacy",
            run_id="run_legacy",
            user_id="u1",
            session_id="s1",
            node_name="assistant",
            event_type="assistant_decision",
            output_summary={
                "context": {
                    "context_schema_version": "context_observability_v1",
                    "budget": {
                        "request_chars": 12,
                        "conversation_chars": 7,
                        "memory_chars": 5,
                        "observations_chars": 3,
                        "tool_spec_chars": 11,
                        "tool_capability_chars": 2,
                        "total_chars": 40,
                        "max_chars": 1000,
                        "compression_stage": "compacted",
                        "compression_reasons": ["conversation_context_compacted"],
                    },
                    "source_counts": {
                        "conversation_turns": 2,
                        "memory_items": 1,
                        "observations": 1,
                        "prompt_tool_specs": 1,
                        "tool_capabilities": 1,
                    },
                    "tool_catalog": {
                        "selected_tool_names": ["web_search"],
                        "fallback_used": False,
                    },
                }
            },
        )
    )
    runtime = AgentGraphRuntime(trace_store=trace_store)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    payload = client.get("/runs/run_legacy/context").json()

    report = payload["context_report_v1"]
    assert report["schema_version"] == "context_report_v1"
    assert report["sections"]["request"]["chars"] == 12
    assert report["sections"]["recent_transcript"]["item_count"] == 2
    assert report["sections"]["memory"]["item_count"] == 1
    assert report["sections"]["tool_schema"]["item_count"] == 1
    assert report["selected_tool_names"] == ["web_search"]
    assert report["compression_stage"] == "compacted"
    assert report["compression_reasons"] == ["conversation_context_compacted"]


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
    assert client.get("/runs/run_missing/context").status_code == 404
    assert client.get("/traces/trace_missing/context").status_code == 404
