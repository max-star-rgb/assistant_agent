"""Stable query contracts for context compilation reports."""

from assistant_agent.observability.trace_query import TraceQueryService
from assistant_agent.observability.trace_store import InMemoryTraceStore, TraceEvent


def test_legacy_context_report_is_exposed_as_sparse_v2() -> None:
    store = InMemoryTraceStore()
    store.append(
        TraceEvent(
            trace_id="trace-context-v1",
            run_id="run-context-v1",
            node_name="assistant",
            event_type="observability",
            canonical_event="context.build.finished",
            output_summary={
                "context_report_v1": {
                    "schema_version": "context_report_v1",
                    "sections": {
                        "request": {
                            "chars": 8,
                            "tokens": None,
                            "item_count": 1,
                            "included": True,
                            "compacted": False,
                            "trimmed": False,
                            "source": "UserRequest.text",
                            "notes": [],
                        },
                        "realtime_task_state": {
                            "chars": 0,
                            "tokens": None,
                            "item_count": 0,
                            "included": False,
                            "compacted": False,
                            "trimmed": False,
                            "source": "request.metadata.realtime_task_state",
                            "notes": [],
                        },
                    },
                    "total_chars": 100,
                    "max_chars": 12_000,
                    "total_tokens": 0,
                    "max_tokens": 0,
                    "selected_tool_names": ["weather"],
                    "context_sources": {
                        "schema_version": "context_source_report_v1",
                        "cache_layout_version": "editable_context_v1",
                    },
                    "accounting_basis": "compiled_chat_request",
                    "budget_estimated_chars": 50,
                    "compiled_message_chars": 80,
                    "compiled_tool_schema_chars": 20,
                    "compiled_response_format_chars": 0,
                }
            },
        )
    )

    result = TraceQueryService(store).context_by_run("run-context-v1")

    assert result is not None
    report = result.context_report_v2
    assert report.schema_version == "context_report_v2"
    assert report.compiled_accounting_status == "available"
    assert report.compiled_request_chars == 100
    assert report.token_accounting_status == "unavailable"
    assert report.compiled_input_tokens is None
    assert report.effective_input_limit is None
    assert report.precompile_estimated_chars == 50
    assert report.precompile_max_chars == 12_000
    assert set(report.sections) == {"request"}
    assert report.sections["request"].estimated_tokens is None
    assert report.context_sources is None
    public_payload = result.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    assert public_payload["context_report_v2"]["schema_version"] == (
        "context_report_v2"
    )
    assert "compiled_input_tokens" not in public_payload["context_report_v2"]
    assert "effective_input_limit" not in public_payload["context_report_v2"]
