import json
import subprocess
import sys
from pathlib import Path

from assistant_agent.services.trace_store import JsonlTraceStore, TraceEvent


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = "scripts/trace_view.py"


def test_trace_view_default_output_shows_run_timeline(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view("run_1", "--trace-path", str(trace_path))

    assert result.returncode == 0
    assert "run run_1 trace trace_1 status=failed" in result.stdout
    assert "react.decision" in result.stdout
    assert "tool.observation" in result.stdout
    assert "provider_timeout" in result.stdout
    assert "output_ref=artifact_tool_1" in result.stdout


def test_trace_view_errors_output_groups_errors_before_timeline(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view("trace_1", "--trace-path", str(trace_path), "--errors")

    assert result.returncode == 0
    assert "Errors" in result.stdout
    assert "Timeline" in result.stdout
    assert result.stdout.index("Errors") < result.stdout.index("Timeline")
    assert result.stdout.index("provider_timeout") < result.stdout.index("Timeline")


def test_trace_view_json_output_is_machine_readable(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view("run_1", "--trace-path", str(trace_path), "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "run_1"
    assert payload["trace_id"] == "trace_1"
    assert payload["status"] == "failed"
    assert payload["error_count"] == 2
    assert payload["events"][0]["canonical_event"] == "run.started"
    assert payload["events"][-1]["error_code"] == "provider_timeout"


def test_trace_view_returns_nonzero_for_missing_id(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view("run_missing", "--trace-path", str(trace_path))

    assert result.returncode == 1
    assert "not found" in result.stderr.lower()
    assert "run_missing" in result.stderr


def _run_trace_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_sample_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    base = {
        "trace_id": "trace_1",
        "run_id": "run_1",
        "user_id": "u1",
        "session_id": "s1",
    }
    store.append(
        TraceEvent(
            **base,
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            status="started",
            span_id="span_run_1",
            attributes={"source": "test"},
        )
    )
    store.append(
        TraceEvent(
            **base,
            node_name="native_runtime",
            event_type="observability",
            canonical_event="llm.chat.finished",
            status="succeeded",
            provider="mock",
            model="mock-chat",
            latency_ms=42,
            span_id="span_llm_1",
            parent_span_id="span_run_1",
            attributes={"iteration": 1},
        )
    )
    store.append(
        TraceEvent(
            **base,
            node_name="native_runtime",
            event_type="assistant_decision",
            canonical_event="react.decision",
            status="tool_call",
            tool_name="product_search",
            span_id="span_decision_1",
            parent_span_id="span_llm_1",
            attributes={"decision_type": "tool_call", "tool_call_id": "call_1"},
        )
    )
    store.append(
        TraceEvent(
            **base,
            node_name="native_runtime",
            event_type="tool_observation",
            canonical_event="tool.observation",
            status="failed",
            tool_name="product_search",
            latency_ms=80,
            span_id="span_observation_1",
            parent_span_id="span_decision_1",
            output_summary={"output_ref": "artifact_tool_1"},
            attributes={"recovery_action": "retry_or_report"},
            error={"code": "provider_timeout", "message": "Provider timed out"},
        )
    )
    store.append(
        TraceEvent(
            **base,
            node_name="runtime",
            event_type="observability",
            canonical_event="run.failed",
            status="failed",
            span_id="span_run_end_1",
            parent_span_id="span_run_1",
            error={"code": "provider_timeout", "message": "Provider timed out"},
        )
    )
