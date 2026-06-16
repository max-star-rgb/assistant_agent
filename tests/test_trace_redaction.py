from multimodal_agent.services.trace_store import InMemoryTraceStore, TraceEvent, trace_debug_summary


def test_trace_store_redacts_sensitive_input_and_output_summaries() -> None:
    trace_store = InMemoryTraceStore()
    raw_base64 = "a" * 120

    trace_store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            node_name="provider_node",
            event_type="tool_failed",
            capability="image_understanding",
            tool_name="vision_understanding",
            provider="qwen",
            model="qwen-vl",
            status="failed",
            error_code="provider_bad_response",
            input_summary={
                "Authorization": "Bearer sk-test",
                "path": "/home/user/private/image.png",
                "image": f"data:image/png;base64,{raw_base64}",
            },
            output_summary={
                "raw_response": {"body": "provider raw response " + ("x" * 1000)},
                "safe": "ok",
            },
            error={"code": "provider_bad_response", "message": "secret=hidden Bearer sk-test"},
        )
    )

    dumped = trace_store.list_by_run("run_1")[0].model_dump_json()

    assert "sk-test" not in dumped
    assert "hidden" not in dumped
    assert raw_base64 not in dumped
    assert "/home/user/private/image.png" not in dumped
    assert "provider raw response" not in dumped
    assert "[redacted]" in dumped


def test_trace_debug_summary_is_redacted() -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            node_name="tool_executor",
            event_type="tool_failed",
            tool_name="product_search",
            provider="http",
            error_code="provider_timeout",
            error={"code": "provider_timeout", "message": "api_key=abc timed out"},
        )
    )

    summary = trace_debug_summary(trace_store.list_by_trace("trace_1"))
    dumped = str(summary)

    assert summary["run_id"] == "run_1"
    assert summary["trace_id"] == "trace_1"
    assert summary["error_count"] == 1
    assert "api_key=abc" not in dumped
    assert "[redacted]" in dumped
