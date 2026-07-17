import json

from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.assistant_run_service import ConversationTurn, InMemoryConversationStore
from assistant_agent.services.trace_conversation import InMemoryTraceConversationStore
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
    turn_summary = trace_summary["turn_summary"]
    assert turn_summary["schema_version"] == "assistant_turn_summary_v1"
    assert turn_summary["terminal_status"] == "completed"
    assert turn_summary["client_type"] == "api"
    assert turn_summary["response_present"] is True
    assert turn_summary["assistant_run_id"] == run_payload["run_id"]
    summary_dump = json.dumps(turn_summary, ensure_ascii=False)
    assert "帮我找相似款" not in summary_dump
    assert run_payload["response_text"] not in summary_dump
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
    assert trace_summary["context"]["context_sources"]["schema_version"] == (
        "context_source_report_v1"
    )
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
                    "context_sources": {
                        "schema_version": "context_source_report_v1",
                        "count_by_kind": {"soul": 1},
                        "chars_by_authority": {"owner_persona": 18},
                        "chars_by_stability": {"semi_stable": 18},
                        "source_issue_count": 1,
                        "source_issue_codes": ["soul_file_unreadable"],
                        "used_last_known_good": True,
                        "source_versions_changed": 1,
                        "omitted_section_count": 0,
                        "cache_layout_version": "editable_context_v1",
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
    assert report["context_sources"]["source_issue_codes"] == [
        "soul_file_unreadable"
    ]
    assert report["context_sources"]["used_last_known_good"] is True


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


def test_trace_query_api_projects_latest_turn_latency(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_latency",
            run_id="assistant_run_latency",
            node_name="agent_service",
            event_type="observability",
            canonical_event="agent_service.turn.finished",
            output_summary={
                "turn_latency": {
                    "schema_version": "agent_service_turn_latency_v1",
                    "status": "sent",
                    "delivery_id": "delivery_1",
                    "session_turn": 1,
                    "chat_index_digest": "digest_1",
                    "gateway_run_id": "gateway_run_1",
                    "assistant_run_id": "assistant_run_latency",
                    "trace_id": "trace_latency",
                    "total_ms": 123,
                    "stages": [],
                    "ack_status": "not_negotiated",
                }
            },
        )
    )
    runtime = AgentGraphRuntime(trace_store=trace_store)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    run_payload = client.get("/runs/assistant_run_latency").json()
    trace_payload = client.get("/traces/trace_latency").json()

    assert run_payload["turn_latency"]["total_ms"] == 123
    assert trace_payload["turn_latency"]["gateway_run_id"] == "gateway_run_1"
    assert "conversation" not in run_payload["turn_latency"]


def test_trace_query_api_projects_latest_turn_summary(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_turn_summary",
            run_id="assistant_run_summary",
            user_id="user_summary",
            session_id="session_summary",
            node_name="runtime",
            event_type="observability",
            canonical_event="assistant.turn.summary",
            status="failed",
            output_summary={
                "turn_summary": {
                    "schema_version": "assistant_turn_summary_v1",
                    "trace_id": "trace_turn_summary",
                    "assistant_run_id": "assistant_run_summary",
                    "gateway_run_id": "gateway_run_summary",
                    "turn_id": "turn_summary",
                    "user_id": "user_summary",
                    "session_id": "session_summary",
                    "session_turn": 3,
                    "client_type": "media_agent",
                    "terminal_status": "failed",
                    "response_present": False,
                    "tool_count": 1,
                    "error_count": 1,
                    "failure_summary": {
                        "code": "provider_network_error",
                        "message": "provider network error",
                    },
                    "latency_summary_ref": {
                        "canonical_event": "agent_service.turn.finished",
                        "delivery_id": "delivery_summary",
                    },
                }
            },
        )
    )
    runtime = AgentGraphRuntime(trace_store=trace_store)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    run_payload = client.get("/runs/assistant_run_summary").json()
    trace_payload = client.get("/traces/trace_turn_summary").json()

    assert run_payload["turn_summary"]["terminal_status"] == "failed"
    assert trace_payload["turn_summary"]["gateway_run_id"] == "gateway_run_summary"
    assert trace_payload["turn_summary"]["latency_summary_ref"]["delivery_id"] == "delivery_summary"
    assert "conversation" not in run_payload["turn_summary"]


def test_trace_conversation_endpoint_is_hidden_when_disabled(monkeypatch) -> None:
    runtime, conversation_store = _trace_conversation_fixture()
    monkeypatch.delenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", raising=False)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: conversation_store)
    client = TestClient(create_app(), client=("127.0.0.1", 50000))

    response = client.get("/traces/trace_content/conversation")

    assert response.status_code == 404


def test_trace_conversation_endpoint_rejects_non_loopback_client(monkeypatch) -> None:
    runtime, conversation_store = _trace_conversation_fixture()
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: conversation_store)
    client = TestClient(create_app(), client=("203.0.113.10", 50000))

    response = client.get("/traces/trace_content/conversation")

    assert response.status_code == 403


def test_trace_conversation_endpoint_returns_matching_turn_without_identity(monkeypatch) -> None:
    runtime, conversation_store = _trace_conversation_fixture()
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: conversation_store)
    client = TestClient(create_app(), client=("127.0.0.1", 50000))

    response = client.get("/traces/trace_content/conversation")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "trace_conversation_view_v1",
        "trace_id": "trace_content",
        "user": {"text": "眼前是什么？", "chars": 6, "truncated": False},
        "assistant": {"text": "眼前是一个杯子。", "chars": 8, "truncated": False},
    }


def test_trace_conversation_endpoint_returns_failed_turn_debug_content(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_failed_content",
            run_id="run_failed_content",
            user_id="user_failed",
            session_id="session_failed",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.failed",
            status="failed",
        )
    )
    runtime = AgentGraphRuntime(trace_store=trace_store)
    conversation_store = InMemoryConversationStore()
    trace_conversation_store = InMemoryTraceConversationStore()
    trace_conversation_store.append(
        user_id="user_failed",
        session_id="session_failed",
        trace_id="trace_failed_content",
        user_text="帮我查一下今天的 AI 新闻",
        assistant_text="请求失败：provider_network_error",
    )
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: conversation_store)
    monkeypatch.setattr(
        routes_agent,
        "get_default_trace_conversation_store",
        lambda: trace_conversation_store,
    )
    client = TestClient(create_app(), client=("127.0.0.1", 50000))

    response = client.get("/traces/trace_failed_content/conversation")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "trace_conversation_view_v1",
        "trace_id": "trace_failed_content",
        "user": {"text": "帮我查一下今天的 AI 新闻", "chars": 14, "truncated": False},
        "assistant": {"text": "请求失败：provider_network_error", "chars": 27, "truncated": False},
    }
    assert conversation_store.get("user_failed", "session_failed") == []


def test_trace_conversation_endpoint_returns_404_for_unknown_trace_or_content(monkeypatch) -> None:
    runtime, conversation_store = _trace_conversation_fixture()
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_default_conversation_store", lambda config=None: conversation_store)
    client = TestClient(create_app(), client=("localhost", 50000))

    unknown_trace = client.get("/traces/trace_missing/conversation")
    conversation_store.clear("user_content", "session_content")
    missing_content = client.get("/traces/trace_content/conversation")

    assert unknown_trace.status_code == 404
    assert missing_content.status_code == 404


def test_trace_query_api_returns_404_for_unknown_ids() -> None:
    client = TestClient(create_app())

    assert client.get("/runs/run_missing").status_code == 404
    assert client.get("/traces/trace_missing").status_code == 404
    assert client.get("/runs/run_missing/context").status_code == 404
    assert client.get("/traces/trace_missing/context").status_code == 404


def _trace_conversation_fixture() -> tuple[AgentGraphRuntime, InMemoryConversationStore]:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_content",
            run_id="run_content",
            user_id="user_content",
            session_id="session_content",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
        )
    )
    runtime = AgentGraphRuntime(trace_store=trace_store)
    conversation_store = InMemoryConversationStore()
    conversation_store.append(
        "user_content",
        "session_content",
        ConversationTurn(
            user_text="眼前是什么？",
            assistant_text="眼前是一个杯子。",
            run_id="run_content",
            trace_id="trace_content",
        ),
    )
    return runtime, conversation_store
