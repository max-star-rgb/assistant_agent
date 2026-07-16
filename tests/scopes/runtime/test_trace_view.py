import subprocess
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlparse

from assistant_agent.services.trace_store import JsonlTraceStore, TraceEvent


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = "scripts/trace_view.py"


def test_trace_view_last_outputs_latest_local_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view("last", "--trace-path", str(trace_path), "--errors")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("run run_new trace trace_new status=completed events=4")
    assert "llm.chat.finished" in result.stdout
    assert "react.decision" in result.stdout
    assert "run_old" not in result.stdout


def test_trace_view_follow_outputs_current_latest_run_once(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_trace_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0
    assert "run run_new trace trace_new status=completed events=4" in result.stdout
    assert "run_old" not in result.stdout


def test_trace_view_last_session_id_selects_latest_matching_session_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_session_sample_trace(trace_path)

    result = _run_trace_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--session-id",
        "debug-session",
        "--errors",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("run run_debug trace trace_debug status=completed events=2")
    assert "run_global_latest" not in result.stdout


def test_trace_view_full_sections_resolve_last_and_render_conversation_timeline_react(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _TraceViewServer(
        {
            "/traces/trace_new": _server_trace_payload(),
            "/traces/trace_new/conversation": {
                "schema_version": "trace_conversation_view_v1",
                "trace_id": "trace_new",
                "user": {"text": "用户原文：帮我找一双白色板鞋", "truncated": False, "chars": 16},
                "assistant": {"text": "助手原文：我会先搜索可选商品。", "truncated": False, "chars": 15},
            },
        }
    )
    server.start()
    try:
        result = _run_trace_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "conversation,timeline,react",
            "--errors",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    conversation_index = result.stdout.index("Conversation")
    timeline_index = result.stdout.index("Timeline")
    react_index = result.stdout.index("ReAct detail")
    assert conversation_index < timeline_index < react_index
    assert "User: 用户原文：帮我找一双白色板鞋" in result.stdout
    assert "Assistant: 助手原文：我会先搜索可选商品。" in result.stdout
    assert "decision=tool_call" in result.stdout
    assert "why=needs product search evidence before continuing" in result.stdout
    assert "validation=accepted" in result.stdout
    assert "tool=product_search" in result.stdout


def test_trace_view_follow_server_outputs_conversation_timeline_react(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _TraceViewServer(
        {
            "/traces/trace_new": _server_trace_payload(),
            "/traces/trace_new/conversation": {
                "schema_version": "trace_conversation_view_v1",
                "trace_id": "trace_new",
                "user": {"text": "用户原文：帮我找一双白色板鞋", "truncated": False, "chars": 16},
                "assistant": {"text": "助手原文：我会先搜索可选商品。", "truncated": False, "chars": 15},
            },
        }
    )
    server.start()
    try:
        result = _run_trace_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "conversation,timeline,react",
            "--errors",
            "--follow",
            "--follow-limit",
            "1",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    conversation_index = result.stdout.index("Conversation")
    timeline_index = result.stdout.index("Timeline")
    react_index = result.stdout.index("ReAct detail")
    assert conversation_index < timeline_index < react_index
    assert "User: 用户原文：帮我找一双白色板鞋" in result.stdout
    assert "decision=tool_call" in result.stdout
    assert "tool=product_search" in result.stdout


def test_trace_view_follow_server_keeps_timeline_when_conversation_is_missing(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _TraceViewServer({"/traces/trace_new": _server_trace_payload()})
    server.start()
    try:
        result = _run_trace_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "conversation,timeline,react",
            "--errors",
            "--follow",
            "--follow-limit",
            "1",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Conversation" in result.stdout
    assert "unavailable" in result.stdout
    assert "Timeline" in result.stdout
    assert "ReAct detail" in result.stdout
    assert "decision=tool_call" in result.stdout


def _run_trace_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class _TraceViewServer:
    def __init__(self, routes: dict[str, dict[str, Any]]) -> None:
        self.routes = routes
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=1)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server hook.
                path = urlparse(self.path).path
                payload = routes.get(path)
                if payload is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json_dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _server_trace_payload() -> dict[str, Any]:
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "trace_id": "trace_new",
        "run_id": "run_new",
        "events": [
            _server_event("run.started", created_at=base_time),
            _server_event(
                "llm.chat.finished",
                status="succeeded",
                provider="mock",
                model="mock-chat",
                latency_ms=42,
                created_at=base_time + timedelta(milliseconds=42),
                attributes={"iteration": 1, "message_kind": "tool_call", "total_tokens": 120},
            ),
            _server_event(
                "react.decision",
                status="tool_call",
                tool_name="product_search",
                created_at=base_time + timedelta(milliseconds=45),
                output_summary={
                    "decision_type": "tool_call",
                    "reason": "needs product search evidence before continuing",
                    "confidence": 0.87,
                    "context": {
                        "budget": {
                            "context_usage_ratio": 0.42,
                            "total_tokens": 420,
                            "max_tokens": 1000,
                        },
                        "source_counts": {"conversation_turns": 1, "memory_items": 0},
                    },
                },
                attributes={"iteration": 1, "decision_type": "tool_call"},
            ),
            _server_event(
                "action.validation.finished",
                status="accepted",
                tool_name="product_search",
                created_at=base_time + timedelta(milliseconds=47),
                output_summary={"validator_result": {"accepted": True, "code": None}},
                attributes={"iteration": 1, "accepted": True, "risk": "external_read"},
            ),
            _server_event(
                "tool.finished",
                status="succeeded",
                tool_name="product_search",
                latency_ms=30,
                created_at=base_time + timedelta(milliseconds=80),
                output_summary={"output_ref": "mock://products/white-sneaker", "result_count": 3},
                attributes={"iteration": 1, "tool_call_id": "call_1"},
            ),
            _server_event(
                "run.completed",
                status="completed",
                created_at=base_time + timedelta(milliseconds=100),
            ),
        ],
    }


def _server_event(
    canonical_event: str,
    *,
    status: str = "started",
    tool_name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    created_at: datetime,
    output_summary: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": "trace_new",
        "run_id": "run_new",
        "node_name": "runtime",
        "event_type": "observability",
        "canonical_event": canonical_event,
        "status": status,
        "tool_name": tool_name,
        "provider": provider,
        "model": model,
        "latency_ms": latency_ms,
        "error_code": None,
        "output_summary": output_summary or {},
        "attributes": attributes or {},
        "created_at": created_at.isoformat(),
    }


def _write_sample_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    for event in (
        _event("trace_old", "run_old", "run.started", created_at=base_time),
        _event(
            "trace_old",
            "run_old",
            "run.completed",
            status="completed",
            created_at=base_time + timedelta(milliseconds=10),
        ),
        _event(
            "trace_new",
            "run_new",
            "run.started",
            created_at=base_time + timedelta(seconds=1),
        ),
        _event(
            "trace_new",
            "run_new",
            "llm.chat.finished",
            status="succeeded",
            provider="mock",
            model="mock-chat",
            latency_ms=42,
            created_at=base_time + timedelta(seconds=1, milliseconds=42),
        ),
        _event(
            "trace_new",
            "run_new",
            "react.decision",
            status="succeeded",
            created_at=base_time + timedelta(seconds=1, milliseconds=45),
            attributes={"decision_type": "tool_call", "iteration": 1},
        ),
        _event(
            "trace_new",
            "run_new",
            "run.completed",
            status="completed",
            created_at=base_time + timedelta(seconds=1, milliseconds=50),
        ),
    ):
        store.append(event)


def _write_session_sample_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    for event in (
        _event(
            "trace_debug",
            "run_debug",
            "run.started",
            session_id="debug-session",
            created_at=base_time,
        ),
        _event(
            "trace_debug",
            "run_debug",
            "run.completed",
            status="completed",
            session_id="debug-session",
            created_at=base_time + timedelta(milliseconds=10),
        ),
        _event(
            "trace_global_latest",
            "run_global_latest",
            "run.started",
            session_id="other-session",
            created_at=base_time + timedelta(seconds=1),
        ),
        _event(
            "trace_global_latest",
            "run_global_latest",
            "run.completed",
            status="completed",
            session_id="other-session",
            created_at=base_time + timedelta(seconds=1, milliseconds=10),
        ),
    ):
        store.append(event)


def _event(
    trace_id: str,
    run_id: str,
    canonical_event: str,
    *,
    status: str = "started",
    provider: str | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    session_id: str | None = None,
    created_at: datetime,
    attributes: dict[str, object] | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id=trace_id,
        run_id=run_id,
        session_id=session_id,
        node_name="runtime",
        event_type="observability",
        canonical_event=canonical_event,
        status=status,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        attributes=attributes or {},
        created_at=created_at,
    )
