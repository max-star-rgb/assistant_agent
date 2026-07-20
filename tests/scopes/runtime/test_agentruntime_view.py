import importlib.util
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
SCRIPT_PATH = "scripts/agentruntime_view.py"


def test_agentruntime_view_last_outputs_latest_local_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_agentruntime_view("last", "--trace-path", str(trace_path), "--errors")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("run run_new trace trace_new status=completed events=4")
    assert "Turn Overview" in result.stdout
    assert "execution=success" in result.stdout
    assert "task_outcome=unknown" in result.stdout
    assert "Decision path" in result.stdout
    assert "LLM chat x1" in result.stdout
    assert "Raw events" not in result.stdout
    assert "llm.chat.finished" not in result.stdout
    assert "react.decision" not in result.stdout
    assert "run_old" not in result.stdout


def test_agentruntime_view_follow_latest_waits_for_new_trace_by_default(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_agentruntime_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-timeout",
        "0",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_agentruntime_view_follow_can_include_existing_latest_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)

    result = _run_agentruntime_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-include-existing",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0
    assert "run run_new trace trace_new status=completed events=4" in result.stdout
    assert "run_old" not in result.stdout


def test_agentruntime_view_follow_include_existing_does_not_print_partial_agent_service_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_agent_service_partial_trace(trace_path)

    result = _run_agentruntime_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-include-existing",
        "--follow-timeout",
        "0",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_agentruntime_view_follow_readiness_waits_for_agent_service_turn_finished() -> None:
    module = _load_agentruntime_view_module()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    partial = [
        _event(
            "trace_agent_service",
            "run_agent_service",
            "run.completed",
            status="completed",
            session_id="agent-service-live",
            created_at=base_time,
        )
    ]
    complete = [
        *partial,
        _event(
            "trace_agent_service",
            "run_agent_service",
            "agent_service.turn.finished",
            status="sent",
            session_id="agent-service-live",
            created_at=base_time + timedelta(milliseconds=1),
        ),
    ]

    assert module._follow_update_ready(partial) is False
    assert module._follow_update_ready(complete) is True


def test_agentruntime_view_follow_readiness_accepts_assistant_turn_summary() -> None:
    module = _load_agentruntime_view_module()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        _event(
            "trace_summary",
            "run_summary",
            "run.started",
            session_id="summary-session",
            created_at=base_time,
        ),
        _event(
            "trace_summary",
            "run_summary",
            "assistant.turn.summary",
            status="summary",
            session_id="summary-session",
            created_at=base_time + timedelta(milliseconds=1),
            output_summary={
                "turn_summary": {
                    "schema_version": "assistant_turn_summary_v1",
                    "trace_id": "trace_summary",
                    "assistant_run_id": "run_summary",
                    "user_id": "u1",
                    "session_id": "summary-session",
                    "client_type": "api",
                    "terminal_status": "completed",
                    "response_present": True,
                    "tool_count": 0,
                    "error_count": 0,
                }
            },
        ),
    ]

    assert module._follow_update_ready(events) is True


def test_agentruntime_view_summary_payload_prefers_turn_summary_session_and_status() -> None:
    module = _load_agentruntime_view_module()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        _event(
            "trace_summary",
            "run_summary",
            "run.started",
            session_id="raw-session",
            created_at=base_time,
        ),
        _event(
            "trace_summary",
            "run_summary",
            "assistant.turn.summary",
            status="summary",
            session_id="summary-session",
            created_at=base_time + timedelta(milliseconds=1),
            output_summary={
                "turn_summary": {
                    "schema_version": "assistant_turn_summary_v1",
                    "trace_id": "trace_summary",
                    "assistant_run_id": "run_summary",
                    "gateway_run_id": "gateway_summary",
                    "turn_id": "turn_summary",
                    "user_id": "u1",
                    "session_id": "summary-session",
                    "session_turn": 2,
                    "client_type": "media_agent",
                    "terminal_status": "cancelled",
                    "response_present": False,
                    "tool_count": 0,
                    "error_count": 1,
                    "failure_summary": {"code": "agent_run_cancelled"},
                    "latency_summary_ref": None,
                }
            },
        ),
    ]

    payload = module._summary_payload(events)

    assert payload["status"] == "cancelled"
    assert payload["session_id"] == "summary-session"
    assert payload["turn_summary"]["client_type"] == "media_agent"
    assert payload["turn_summary"]["gateway_run_id"] == "gateway_summary"


def test_agentruntime_view_uses_turn_summary_without_rendering_summary_block(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    store = JsonlTraceStore(trace_path)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    for event in (
        _event(
            "trace_summary",
            "run_summary",
            "run.started",
            session_id="raw-session",
            created_at=base_time,
        ),
        _event(
            "trace_summary",
            "run_summary",
            "assistant.turn.summary",
            status="summary",
            session_id="summary-session",
            created_at=base_time + timedelta(milliseconds=1),
            output_summary={
                "turn_summary": {
                    "schema_version": "assistant_turn_summary_v1",
                    "trace_id": "trace_summary",
                    "assistant_run_id": "run_summary",
                    "gateway_run_id": "gateway_summary",
                    "user_id": "u1",
                    "session_id": "summary-session",
                    "client_type": "media_agent",
                    "terminal_status": "cancelled",
                    "response_present": False,
                    "tool_count": 0,
                    "error_count": 1,
                }
            },
        ),
    ):
        store.append(event)

    result = _run_agentruntime_view("last", "--trace-path", str(trace_path), "--sections", "timeline")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith(
        "run run_summary trace trace_summary status=cancelled events=2 errors=1 client=media_agent"
    )
    assert "Turn summary" not in result.stdout
    assert "Raw events" in result.stdout
    assert "assistant.turn.summary" in result.stdout


def test_agentruntime_view_follow_latest_keeps_global_session_visibility_by_default() -> None:
    module = _load_agentruntime_view_module()
    args = module.build_parser().parse_args(["last", "--follow"])

    assert module._follow_lookup_session_id(args, locked_session_id=None) is None

    locked = module._next_locked_follow_session_id(
        args,
        locked_session_id=None,
        current_session_id="agent-service-session",
    )

    assert locked is None
    assert module._follow_lookup_session_id(args, locked_session_id=locked) is None


def test_agentruntime_view_follow_all_sessions_keeps_global_latest_mode() -> None:
    module = _load_agentruntime_view_module()
    args = module.build_parser().parse_args(["last", "--follow", "--follow-all-sessions"])

    locked = module._next_locked_follow_session_id(
        args,
        locked_session_id=None,
        current_session_id="agent-service-session",
    )

    assert locked is None
    assert module._follow_lookup_session_id(args, locked_session_id="ignored") is None


def test_agentruntime_view_follow_without_session_id_prints_initial_session_separator_by_default(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_session_sample_trace(trace_path)

    result = _run_agentruntime_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-include-existing",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith(
        "\n================ SESSION other-session ================\n"
        "run run_global_latest trace trace_global_latest status=completed events=2"
    )


def test_agentruntime_view_follow_default_prints_separator_after_session_switch() -> None:
    module = _load_agentruntime_view_module()
    args = module.build_parser().parse_args(["last", "--follow"])

    assert module._should_print_session_separator(
        args,
        printed_any=False,
        session_changed=True,
    ) is True
    assert module._should_print_session_separator(
        args,
        printed_any=True,
        session_changed=True,
    ) is True


def test_agentruntime_view_follow_latest_collects_all_new_runs_between_polls(tmp_path: Path) -> None:
    module = _load_agentruntime_view_module()
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_session_sample_trace(trace_path)
    args = module.build_parser().parse_args(["last", "--trace-path", str(trace_path), "--follow"])

    groups = module._follow_event_groups(args, suppressed_run_ids=set())

    assert [group[0].run_id for group in groups] == ["run_debug", "run_global_latest"]


def test_agentruntime_view_follow_can_show_session_separator(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_session_sample_trace(trace_path)

    result = _run_agentruntime_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--follow",
        "--follow-include-existing",
        "--show-session-banner",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith(
        "\n================ SESSION other-session ================\n"
        "run run_global_latest trace trace_global_latest status=completed events=2"
    )


def test_agentruntime_view_follow_session_id_filters_without_session_separator(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_session_sample_trace(trace_path)

    result = _run_agentruntime_view(
        "last",
        "--trace-path",
        str(trace_path),
        "--session-id",
        "debug-session",
        "--follow",
        "--follow-include-existing",
        "--follow-limit",
        "1",
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("run run_debug trace trace_debug status=completed events=2")
    assert "SESSION " not in result.stdout
    assert "run_global_latest" not in result.stdout


def test_agentruntime_view_last_session_id_selects_latest_matching_session_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_session_sample_trace(trace_path)

    result = _run_agentruntime_view(
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


def test_agentruntime_view_full_sections_fetch_and_render_conversation_decision_raw_events(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer(
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
        result = _run_agentruntime_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "overview,conversation,decision,timeline",
            "--errors",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    overview_index = result.stdout.index("Turn Overview")
    conversation_index = result.stdout.index("Conversation")
    decision_index = result.stdout.index("Decision Trace")
    raw_index = result.stdout.index("Raw events")
    assert overview_index < conversation_index < decision_index < raw_index
    assert "ReAct detail" not in result.stdout
    assert "Turn summary" not in result.stdout
    assert "User: 用户原文：帮我找一双白色板鞋" in result.stdout
    assert "Assistant: 助手原文：我会先搜索可选商品。" in result.stdout
    assert "Decision  tool_call product_search" in result.stdout
    assert "Tool      product_search 30ms succeeded 3 results" in result.stdout
    assert "decision=tool_call" in result.stdout
    assert "why=needs product search evidence before continuing" in result.stdout
    assert "validation=accepted" in result.stdout
    assert "tool=product_search" in result.stdout


def test_agentruntime_view_follow_server_accepts_legacy_react_section_as_decision_trace(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer(
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
        result = _run_agentruntime_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "conversation,timeline,react",
            "--errors",
            "--follow",
            "--follow-include-existing",
            "--follow-limit",
            "1",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    conversation_index = result.stdout.index("Conversation")
    decision_index = result.stdout.index("Decision Trace")
    raw_index = result.stdout.index("Raw events")
    assert conversation_index < decision_index < raw_index
    assert "ReAct detail" not in result.stdout
    assert "Turn summary" not in result.stdout
    assert "User: 用户原文：帮我找一双白色板鞋" in result.stdout
    assert "Decision  tool_call product_search" in result.stdout
    assert "decision=tool_call" in result.stdout
    assert "why=needs product search evidence before continuing" in result.stdout
    assert "tool=product_search" in result.stdout


def test_agentruntime_view_full_sections_render_unavailable_conversation_when_endpoint_missing(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer({"/traces/trace_new": _server_trace_payload()})
    server.start()
    try:
        result = _run_agentruntime_view(
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
    assert "Conversation" in result.stdout
    assert "unavailable" in result.stdout
    assert "conversation endpoint returned 404" in result.stdout
    assert "Decision Trace" in result.stdout
    assert "Raw events" in result.stdout
    assert "ReAct detail" not in result.stdout
    assert "Turn summary" not in result.stdout
    assert "decision=tool_call" in result.stdout


def test_agentruntime_view_turn_latency_hides_stage_rows_by_default(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer({"/traces/trace_new": _server_trace_payload_with_turn_latency()})
    server.start()
    try:
        result = _run_agentruntime_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "timeline",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Turn latency" in result.stdout
    assert "bottleneck=llm_chat[1] 6906ms (97.9%)" in result.stdout
    assert "ACK: not_negotiated" in result.stdout
    assert "Video: source=realtime_video_context pending=1 in_flight=true fallback=false" in result.stdout
    assert "  Stages" not in result.stdout
    assert "    llm_chat[1]" not in result.stdout
    assert "Raw events" in result.stdout
    assert "llm.chat.finished" in result.stdout


def test_agentruntime_view_default_overview_surfaces_turn_diagnostic_flags(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer({"/traces/trace_new": _server_trace_payload_with_diagnostic_risks()})
    server.start()
    try:
        result = _run_agentruntime_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Turn Overview" in result.stdout
    assert "execution=success" in result.stdout
    assert "delivery=success" in result.stdout
    assert "task_outcome=unknown" in result.stdout
    assert "ux_outcome=unknown" in result.stdout
    assert "Total latency    27790ms" in result.stdout
    assert "first_text=unknown" in result.stdout
    assert "first_audio=unknown" in result.stdout
    assert "LLM wall         7085ms provider=245ms overhead=6840ms" in result.stdout
    assert "Context peak     81.3%" in result.stdout
    assert "P0 LLM overhead 6840ms exceeds provider latency" in result.stdout
    assert "P1 Context peak 81.3%" in result.stdout
    assert "P1 realtime first-text/first-audio latency is missing" in result.stdout
    assert "Raw events" not in result.stdout
    assert "llm.chat.finished" not in result.stdout


def test_agentruntime_view_latency_stages_flag_outputs_stage_rows(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer({"/traces/trace_new": _server_trace_payload_with_turn_latency()})
    server.start()
    try:
        result = _run_agentruntime_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "timeline",
            "--latency-stages",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    assert "  Stages" in result.stdout
    assert (
        "    llm_chat[1] 6906ms iteration=1 provider=deepseek "
        "model=deepseek-v4-flash provider_latency=218ms"
    ) in result.stdout


def test_agentruntime_view_renders_tool_exposure_from_context_machine_log(tmp_path: Path) -> None:
    trace_path = tmp_path / "graph_trace.jsonl"
    _write_sample_trace(trace_path)
    server = _AgentRuntimeViewServer({"/traces/trace_new": _server_trace_payload_with_tool_exposure()})
    server.start()
    try:
        result = _run_agentruntime_view(
            "last",
            "--trace-path",
            str(trace_path),
            "--server",
            server.url,
            "--sections",
            "timeline",
        )
    finally:
        server.stop()

    assert result.returncode == 0
    assert result.stderr == ""
    assert "context.build.finished" in result.stdout
    assert 'selected_tools=["web_search", "video_understanding"]' in result.stdout
    assert 'excluded_tools={"price_compare": ["entry_profile_not_exposed"]}' in result.stdout


def _run_agentruntime_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_agentruntime_view_module():
    spec = importlib.util.spec_from_file_location("agentruntime_view_test_module", REPO_ROOT / SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _AgentRuntimeViewServer:
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


def _server_trace_payload_with_turn_latency() -> dict[str, Any]:
    payload = _server_trace_payload()
    payload["turn_latency"] = {
        "schema_version": "agent_service_turn_latency_v1",
        "status": "sent",
        "delivery_id": "delivery_x",
        "session_turn": 4,
        "total_ms": 7052,
        "trace_id": "trace_new",
        "gateway_run_id": "gateway_run_x",
        "assistant_run_id": "run_new",
        "bottleneck": "llm_chat[1]",
        "bottleneck_ms": 6906,
        "bottleneck_share_pct": 97.9,
        "ack_status": "not_negotiated",
        "stages": [
            {"name": "entry_parse", "duration_ms": 0},
            {
                "name": "llm_chat[1]",
                "duration_ms": 6906,
                "iteration": 1,
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "provider_latency_ms": 218,
            },
        ],
        "video": {
            "source": "realtime_video_context",
            "pending_count": 1,
            "in_flight": True,
            "fallback_used": False,
        },
    }
    return payload


def _server_trace_payload_with_tool_exposure() -> dict[str, Any]:
    payload = _server_trace_payload()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    payload["events"].insert(
        1,
        _server_event(
            "context.build.finished",
            status="succeeded",
            created_at=base_time + timedelta(milliseconds=20),
            output_summary={
                "context": {
                    "tool_catalog": {
                        "selected_tool_names": ["web_search", "video_understanding"],
                    },
                    "run_tool_set": {
                        "excluded_reasons": {
                            "price_compare": ["entry_profile_not_exposed"],
                        },
                    },
                },
            },
        ),
    )
    return payload


def _server_trace_payload_with_diagnostic_risks() -> dict[str, Any]:
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        _server_event("run.started", created_at=base_time),
        _server_event(
            "context.build.finished",
            status="succeeded",
            created_at=base_time + timedelta(milliseconds=30),
            output_summary={
                "context": {
                    "budget": {
                        "context_usage_ratio": 0.813,
                        "total_tokens": 813,
                        "max_tokens": 1000,
                    },
                    "source_counts": {
                        "prompt_tool_specs": 12,
                        "tool_observations": 5,
                        "conversation_turns": 1,
                    },
                },
            },
        ),
        _server_event(
            "llm.chat.finished",
            status="succeeded",
            provider="deepseek",
            model="deepseek-chat",
            latency_ms=7085,
            created_at=base_time + timedelta(milliseconds=7115),
            attributes={"iteration": 4, "wall_latency_ms": 7085, "provider_latency_ms": 245},
        ),
        _server_event(
            "react.decision",
            status="tool_call",
            tool_name="web_search",
            created_at=base_time + timedelta(milliseconds=7120),
            output_summary={"decision_type": "tool_call", "reason": "needs web evidence"},
            attributes={"iteration": 1},
        ),
        _server_event(
            "tool.finished",
            status="succeeded",
            tool_name="web_search",
            latency_ms=2160,
            created_at=base_time + timedelta(milliseconds=9280),
            output_summary={"result_count": 5},
            attributes={"iteration": 1, "tool_call_id": "search_1"},
        ),
        _server_event(
            "tool.finished",
            status="succeeded",
            tool_name="web_search",
            latency_ms=3180,
            created_at=base_time + timedelta(milliseconds=12460),
            output_summary={"result_count": 5},
            attributes={"iteration": 1, "tool_call_id": "search_2"},
        ),
        _server_event(
            "tool.finished",
            status="succeeded",
            tool_name="web_search",
            latency_ms=3220,
            created_at=base_time + timedelta(milliseconds=15680),
            output_summary={"result_count": 5},
            attributes={"iteration": 1, "tool_call_id": "search_3"},
        ),
        _server_event(
            "tool.finished",
            status="succeeded",
            tool_name="web_fetch",
            latency_ms=1600,
            created_at=base_time + timedelta(milliseconds=17280),
            output_summary={"item_count": 1},
            attributes={"iteration": 2, "tool_call_id": "fetch_1"},
        ),
        _server_event(
            "tool.finished",
            status="succeeded",
            tool_name="web_fetch",
            latency_ms=1870,
            created_at=base_time + timedelta(milliseconds=19150),
            output_summary={"item_count": 1},
            attributes={"iteration": 2, "tool_call_id": "fetch_2"},
        ),
        _server_event(
            "run.completed",
            status="completed",
            created_at=base_time + timedelta(milliseconds=27790),
        ),
    ]
    return {
        "trace_id": "trace_new",
        "run_id": "run_new",
        "status": "completed",
        "duration_ms": 27790,
        "events": events,
        "turn_latency": {
            "schema_version": "agent_service_turn_latency_v1",
            "status": "sent",
            "delivery_id": "delivery_x",
            "session_turn": 1,
            "total_ms": 27790,
            "trace_id": "trace_new",
            "gateway_run_id": "gateway_run_x",
            "assistant_run_id": "run_new",
            "ack_status": "not_negotiated",
            "user_visible_event_count": 26,
        },
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


def _write_agent_service_partial_trace(path: Path) -> None:
    store = JsonlTraceStore(path)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    for event in (
        _event(
            "trace_agent_service",
            "run_agent_service",
            "conversation.prepare.finished",
            status="succeeded",
            session_id="agent-service-live",
            created_at=base_time,
        ),
        _event(
            "trace_agent_service",
            "run_agent_service",
            "run.started",
            session_id="agent-service-live",
            created_at=base_time + timedelta(milliseconds=1),
        ),
        _event(
            "trace_agent_service",
            "run_agent_service",
            "context.report",
            status="succeeded",
            session_id="agent-service-live",
            created_at=base_time + timedelta(milliseconds=2),
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
    output_summary: dict[str, object] | None = None,
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
        output_summary=output_summary or {},
        created_at=created_at,
    )
