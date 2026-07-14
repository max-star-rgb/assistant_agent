import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    assert "at=42ms" in result.stdout
    assert "gap=38ms" in result.stdout


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


def test_trace_view_server_output_reads_running_api() -> None:
    payload = {
        "trace_id": "trace_http",
        "run_id": "run_http",
        "error_count": 0,
        "events": [
            {
                "canonical_event": "run.started",
                "status": "started",
            },
            {
                "canonical_event": "run.completed",
                "status": "completed",
            },
        ],
    }

    with _serve_json({"/traces/trace_http": payload}) as server_url:
        result = _run_trace_view("trace_http", "--server", server_url)

    assert result.returncode == 0
    assert "run run_http trace trace_http status=completed events=2 errors=0" in result.stdout
    assert "run.started" in result.stdout
    assert "run.completed" in result.stdout


def test_trace_view_server_output_renders_turn_latency_before_timeline() -> None:
    payload = _server_trace_payload_with_latency()

    with _serve_json({"/traces/trace_http": payload}) as server_url:
        result = _run_trace_view("trace_http", "--server", server_url)

    assert result.returncode == 0
    assert "Turn latency" in result.stdout
    assert "delivery=delivery_1" in result.stdout
    assert "gateway_run=gateway_run_1" in result.stdout
    assert "assistant_run=run_http" in result.stdout
    assert "total=220ms" in result.stdout
    assert "bottleneck=llm_chat[1] 90ms (40.9%)" in result.stdout
    assert "llm_chat[1]" in result.stdout
    assert "unattributed" in result.stdout
    assert "ACK: acked 12ms" in result.stdout
    assert "source=rolling_observation" in result.stdout
    assert "snapshot_age=45ms" in result.stdout
    assert "provider=qwen" in result.stdout
    assert result.stdout.index("Turn latency") < result.stdout.index("Timeline")
    assert result.stdout.index("Timeline") < result.stdout.index("run.started")


def test_trace_view_server_json_preserves_structured_turn_latency() -> None:
    payload = _server_trace_payload_with_latency()

    with _serve_json({"/traces/trace_http": payload}) as server_url:
        result = _run_trace_view("trace_http", "--server", server_url, "--json")

    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["turn_latency"]["total_ms"] == 220
    assert summary["turn_latency"]["stages"][1]["provider"] == "qwen"
    assert summary["turn_latency"]["video"]["snapshot_sequence"] == 7


def test_trace_view_can_explicitly_include_current_conversation() -> None:
    payload = _server_trace_payload_with_latency()
    conversation = {
        "schema_version": "trace_conversation_view_v1",
        "trace_id": "trace_http",
        "user": {"text": "眼前是什么？", "chars": 6, "truncated": False},
        "assistant": {"text": "眼前是一个杯子。", "chars": 8, "truncated": False},
    }

    with _serve_json(
        {
            "/traces/trace_http": payload,
            "/traces/trace_http/conversation": conversation,
        }
    ) as server_url:
        human = _run_trace_view(
            "trace_http",
            "--server",
            server_url,
            "--include-conversation",
        )
        machine = _run_trace_view(
            "trace_http",
            "--server",
            server_url,
            "--include-conversation",
            "--json",
        )

    assert human.returncode == 0
    assert "Conversation" in human.stdout
    assert "User: 眼前是什么？" in human.stdout
    assert "Assistant: 眼前是一个杯子。" in human.stdout
    assert human.stdout.index("Turn latency") < human.stdout.index("Conversation")
    assert human.stdout.index("Conversation") < human.stdout.index("Timeline")
    assert json.loads(machine.stdout)["conversation"] == conversation


def test_trace_view_rejects_conversation_without_server() -> None:
    result = _run_trace_view("trace_1", "--include-conversation")

    assert result.returncode != 0
    assert "--server" in result.stderr


def test_trace_view_rejects_non_loopback_conversation_server_before_network() -> None:
    result = _run_trace_view(
        "trace_1",
        "--server",
        "http://203.0.113.10:9",
        "--include-conversation",
    )

    assert result.returncode != 0
    assert "loopback" in result.stderr.lower()
    assert "connection refused" not in result.stderr.lower()


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


@contextmanager
def _serve_json(routes: dict[str, dict[str, object]]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload = routes.get(self.path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"detail":"not found"}')
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _write_sample_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
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
            created_at=started_at,
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
            created_at=started_at + timedelta(milliseconds=42),
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
            created_at=started_at + timedelta(milliseconds=47),
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
            created_at=started_at + timedelta(milliseconds=85),
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
            created_at=started_at + timedelta(milliseconds=90),
        )
    )


def _server_trace_payload_with_latency() -> dict[str, object]:
    return {
        "trace_id": "trace_http",
        "run_id": "run_http",
        "error_count": 0,
        "turn_latency": {
            "schema_version": "agent_service_turn_latency_v1",
            "status": "sent",
            "delivery_id": "delivery_1",
            "session_turn": 2,
            "chat_index_digest": "digest_1",
            "turn_id": "turn_1",
            "gateway_run_id": "gateway_run_1",
            "assistant_run_id": "run_http",
            "trace_id": "trace_http",
            "total_ms": 220,
            "stages": [
                {"name": "entry_parse", "duration_ms": 4, "critical_path": True},
                {
                    "name": "llm_chat[1]",
                    "duration_ms": 90,
                    "critical_path": True,
                    "iteration": 1,
                    "provider": "qwen",
                    "model": "qwen-plus",
                    "provider_latency_ms": 82,
                },
                {"name": "unattributed", "duration_ms": 15, "critical_path": True},
            ],
            "bottleneck": "llm_chat[1]",
            "bottleneck_ms": 90,
            "bottleneck_share_pct": 40.9,
            "unattributed_ms": 15,
            "ack_status": "acked",
            "ack_latency_ms": 12,
            "terminal_stage": "ack_received",
            "video": {
                "source": "rolling_observation",
                "snapshot_age_ms": 45,
                "observation_latency_ms": 180,
                "pending_count": 1,
                "in_flight": False,
                "fallback_used": False,
                "snapshot_sequence": 7,
                "provider": "qwen",
                "model": "qwen-vl-max",
            },
        },
        "events": [
            {"canonical_event": "run.started", "status": "started"},
            {"canonical_event": "run.completed", "status": "completed"},
        ],
    }
