from assistant_agent.services.trace_query import TraceQueryService
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent


def test_run_summary_query_returns_debug_summary() -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(trace_id="trace_1", run_id="run_1", node_name="detect_intent", event_type="node_finished")
    )
    trace_store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            node_name="search_node",
            event_type="tool_failed",
            capability="product_search",
            tool_name="product_search",
            provider="mock",
            status="failed",
            error_code="provider_call_limit_exceeded",
            error={"code": "provider_call_limit_exceeded", "retry_count": 0},
        )
    )

    summary = TraceQueryService(trace_store).run_summary("run_1")

    assert summary is not None
    assert summary.run_id == "run_1"
    assert summary.trace_id == "trace_1"
    assert summary.node_path == ["detect_intent"]
    assert summary.tools == ["product_search"]
    assert summary.providers == ["mock"]
    assert summary.error_count == 1
    assert summary.budget_exceeded is True


def test_run_and_trace_summary_expose_latest_context_summary() -> None:
    trace_store = InMemoryTraceStore()
    context = {
        "budget": {"total_chars": 123, "observations_chars": 45},
        "source_counts": {"observations": 1, "tool_specs": 3},
        "compaction": {
            "compacted_observations": 1,
            "original_observation_chars": 500,
            "compacted_observation_chars": 120,
        },
        "tool_catalog": {
            "total_tool_count": 4,
            "prompt_tool_count": 2,
            "filtered_tool_count": 2,
            "selected_tool_names": ["product_search", "price_compare"],
            "selection_reasons": ["price_compare_keyword"],
            "fallback_used": False,
        },
    }
    trace_store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            node_name="assistant",
            event_type="assistant_decision",
            status="final_answer",
            output_summary={"decision_type": "final_answer", "context": context},
        )
    )

    service = TraceQueryService(trace_store)
    run_summary = service.run_summary("run_1")
    trace_summary = service.trace_summary("trace_1")

    assert run_summary is not None
    assert trace_summary is not None
    assert run_summary.context == context
    assert trace_summary.context == context
    assert trace_summary.events[0]["output_summary"]["context"] == context


def test_trace_summary_query_returns_events_without_raw_payloads() -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            node_name="vision_node",
            event_type="tool_failed",
            capability="image_understanding",
            tool_name="vision_understanding",
            provider="qwen",
            input_summary={"prompt": "hello", "Authorization": "Bearer sk-test"},
            output_summary={"raw_response": "x" * 500},
            error={"code": "provider_bad_response", "message": "Bearer sk-test"},
        )
    )

    summary = TraceQueryService(trace_store).trace_summary("trace_1")

    assert summary is not None
    dumped = summary.model_dump_json()
    assert "sk-test" not in dumped
    assert "raw_response" not in dumped
    assert summary.events[0]["tool_name"] == "vision_understanding"


def test_trace_summary_context_omits_raw_payload_keys() -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            node_name="assistant",
            event_type="assistant_decision",
            output_summary={
                "decision_type": "final_answer",
                "context": {
                    "budget": {"total_chars": 100},
                    "raw_provider_payload": {"api_key": "sk-test", "body": "raw"},
                    "media": {
                        "image_base64": "data:image/png;base64," + ("A" * 200),
                        "safe_ref": "artifact://image/1",
                    },
                },
            },
        )
    )

    summary = TraceQueryService(trace_store).trace_summary("trace_1")

    assert summary is not None
    dumped = summary.model_dump_json()
    assert "raw_provider_payload" not in dumped
    assert "image_base64" not in dumped
    assert "sk-test" not in dumped
    assert summary.context["media"]["safe_ref"] == "artifact://image/1"


def test_tool_calls_query_returns_none_for_missing_run() -> None:
    trace_store = InMemoryTraceStore()

    assert TraceQueryService(trace_store).tool_calls_by_run("missing") is None
