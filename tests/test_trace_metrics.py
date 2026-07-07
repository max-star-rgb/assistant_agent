import json
import subprocess
import sys
from pathlib import Path

from assistant_agent.services.trace_metrics import build_trace_metrics, filter_trace_events, load_trace_events
from assistant_agent.services.trace_store import JsonlTraceStore, TraceEvent


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = "scripts/trace_metrics.py"


def test_build_trace_metrics_summarizes_runs_tools_llm_context_gateway_and_memory() -> None:
    events = _sample_events()

    metrics = build_trace_metrics(events)

    assert metrics["event_count"] == len(events)
    assert metrics["trace_count"] == 3
    assert metrics["run"]["count"] == 3
    assert metrics["run"]["completed"] == 1
    assert metrics["run"]["failed"] == 1
    assert metrics["run"]["cancelled"] == 1
    assert metrics["run"]["success_rate"] == 0.3333
    assert metrics["errors"]["count"] == 2
    assert metrics["errors"]["by_code"] == {"provider_timeout": 2}
    assert metrics["tools"]["total_calls"] == 1
    assert metrics["tools"]["by_tool"]["product_search"]["call_count"] == 1
    assert metrics["tools"]["by_tool"]["product_search"]["failure_count"] == 1
    assert metrics["tools"]["by_tool"]["product_search"]["failure_rate"] == 1.0
    assert metrics["tools"]["by_tool"]["product_search"]["retry_count"] == 1
    assert metrics["tools"]["by_tool"]["product_search"]["latency_ms"]["avg"] == 80.0
    assert metrics["llm"]["call_count"] == 2
    assert metrics["llm"]["provider_counts"] == {"mock": 2}
    assert metrics["llm"]["direct_answer_count"] == 1
    assert metrics["llm"]["native_tool_call_count"] == 1
    assert metrics["llm"]["total_tokens"] == 42
    assert metrics["context"]["sample_count"] == 1
    assert metrics["context"]["average_budget_ratio"] == 0.75
    assert metrics["context"]["compaction_triggered_count"] == 1
    assert metrics["context"]["total_tokens"] == 750
    assert metrics["gateway"]["cancel_count"] == 1
    assert metrics["gateway"]["cancel_sources"] == {"hangup": 1}
    assert metrics["memory"]["save_count"] == 1
    assert metrics["memory"]["save_candidate_count"] == 2
    assert metrics["memory"]["saved_count"] == 1
    assert metrics["memory"]["rejected_count"] == 1


def test_trace_metrics_loads_and_filters_jsonl_events(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    events = load_trace_events(trace_path)
    user_events = filter_trace_events(events, user_id="u1")
    session_events = filter_trace_events(events, session_id="s_cancel")

    assert len(events) == len(_sample_events())
    assert {event.run_id for event in user_events} == {"run_success", "run_failed"}
    assert {event.run_id for event in session_events} == {"run_cancelled"}


def test_trace_metrics_script_outputs_human_summary(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_metrics("--trace-path", str(trace_path))

    assert result.returncode == 0
    assert "runs=3 completed=1 failed=1 cancelled=1" in result.stdout
    assert "success_rate=33.3%" in result.stdout
    assert "llm calls=2" in result.stdout
    assert "product_search calls=1 failed=1" in result.stdout
    assert "context samples=1 avg_budget=75.0%" in result.stdout
    assert "gateway cancels=1 sources=hangup:1" in result.stdout
    assert "memory retrievals=1 saves=1 candidates=2 saved=1 rejected=1" in result.stdout


def test_trace_metrics_script_outputs_json_summary(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_metrics("--trace-path", str(trace_path), "--user-id", "u1", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["count"] == 2
    assert payload["run"]["completed"] == 1
    assert payload["run"]["failed"] == 1
    assert payload["run"]["cancelled"] == 0
    assert payload["gateway"]["cancel_count"] == 0


def _run_trace_metrics(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_sample_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    for event in _sample_events():
        store.append(event)


def _sample_events() -> list[TraceEvent]:
    return [
        TraceEvent(
            trace_id="trace_success",
            run_id="run_success",
            user_id="u1",
            session_id="s_success",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            status="started",
        ),
        TraceEvent(
            trace_id="trace_success",
            run_id="run_success",
            user_id="u1",
            session_id="s_success",
            node_name="context",
            event_type="observability",
            canonical_event="context.build.finished",
            status="succeeded",
            output_summary={
                "context": {
                    "budget": {
                        "context_usage_ratio": 0.75,
                        "compaction_triggered": True,
                        "total_tokens": 750,
                        "max_tokens": 1000,
                    }
                }
            },
        ),
        TraceEvent(
            trace_id="trace_success",
            run_id="run_success",
            user_id="u1",
            session_id="s_success",
            node_name="llm",
            event_type="observability",
            canonical_event="llm.chat.finished",
            status="succeeded",
            provider="mock",
            model="mock-chat",
            latency_ms=100,
            attributes={"message_kind": "content", "total_tokens": 21},
        ),
        TraceEvent(
            trace_id="trace_success",
            run_id="run_success",
            user_id="u1",
            session_id="s_success",
            node_name="memory",
            event_type="observability",
            canonical_event="memory.load.finished",
            status="succeeded",
            attributes={"retrieval_count": 1},
        ),
        TraceEvent(
            trace_id="trace_success",
            run_id="run_success",
            user_id="u1",
            session_id="s_success",
            node_name="memory",
            event_type="observability",
            canonical_event="memory.save.finished",
            status="succeeded",
            attributes={"save_candidate_count": 2, "saved_count": 1, "rejected_count": 1},
        ),
        TraceEvent(
            trace_id="trace_success",
            run_id="run_success",
            user_id="u1",
            session_id="s_success",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
            status="completed",
        ),
        TraceEvent(
            trace_id="trace_failed",
            run_id="run_failed",
            user_id="u1",
            session_id="s_failed",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            status="started",
        ),
        TraceEvent(
            trace_id="trace_failed",
            run_id="run_failed",
            user_id="u1",
            session_id="s_failed",
            node_name="llm",
            event_type="observability",
            canonical_event="llm.chat.finished",
            status="succeeded",
            provider="mock",
            model="mock-chat",
            latency_ms=50,
            attributes={"message_kind": "tool_calls", "total_tokens": 21},
        ),
        TraceEvent(
            trace_id="trace_failed",
            run_id="run_failed",
            user_id="u1",
            session_id="s_failed",
            node_name="tool",
            event_type="tool_observation",
            canonical_event="tool.observation",
            status="failed",
            tool_name="product_search",
            latency_ms=80,
            attributes={"retry_count": 1},
            error={"code": "provider_timeout", "message": "Provider timed out"},
        ),
        TraceEvent(
            trace_id="trace_failed",
            run_id="run_failed",
            user_id="u1",
            session_id="s_failed",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.failed",
            status="failed",
            error={"code": "provider_timeout", "message": "Provider timed out"},
        ),
        TraceEvent(
            trace_id="trace_cancelled",
            run_id="run_cancelled",
            user_id="u2",
            session_id="s_cancel",
            node_name="gateway",
            event_type="observability",
            canonical_event="gateway.turn.started",
            status="started",
        ),
        TraceEvent(
            trace_id="trace_cancelled",
            run_id="run_cancelled",
            user_id="u2",
            session_id="s_cancel",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            status="started",
        ),
        TraceEvent(
            trace_id="trace_cancelled",
            run_id="run_cancelled",
            user_id="u2",
            session_id="s_cancel",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.cancelled",
            status="cancelled",
            attributes={"cancel_source": "hangup"},
        ),
    ]
