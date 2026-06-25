import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


DEMO_SCRIPT = Path("scripts/run_client.py")
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Pure helper / unit tests (no network)
# --------------------------------------------------------------------------- #


def test_run_client_redacts_signed_image_urls_for_display() -> None:
    module = _load_demo_module()

    redacted = module._safe_display_value(
        "https://example.com/image.png?X-Tos-Algorithm=TOS4-HMAC-SHA256&X-Tos-Credential=secret&X-Tos-Signature=sig"
    )

    assert redacted == "https://example.com/image.png?[signed-url-redacted]"
    assert "secret" not in redacted
    assert "Signature" not in redacted


def test_run_client_omits_duplicate_tool_summary() -> None:
    module = _load_demo_module()

    summary = module._compact_tool_result_summary(
        {"summary": "工具摘要和最终回答相同"},
        response_text="工具摘要和最终回答相同",
    )

    assert summary == ""


def test_run_client_compact_product_summary_includes_url() -> None:
    module = _load_demo_module()

    summary = module._compact_tool_result_summary(
        {
            "total": 1,
            "items": [
                {
                    "title": "小米17 手机",
                    "price": 3999,
                    "currency": "CNY",
                    "product_url": "https://item.example/xiaomi17",
                }
            ],
        }
    )

    assert "showing 1 of 1" in summary
    assert "1: 小米17 手机" in summary
    assert "3999 CNY" in summary
    assert "https://item.example/xiaomi17" in summary


def test_run_client_product_summary_lists_multiple_items_without_240_char_cutoff() -> None:
    module = _load_demo_module()
    long_title = "超长标题" * 40

    summary = module._compact_tool_result_summary(
        {
            "total": 6,
            "items": [
                {
                    "title": long_title,
                    "price": 10 + index,
                    "currency": "CNY",
                    "product_url": f"https://item.example/{index}",
                }
                for index in range(6)
            ],
        }
    )

    assert "showing 5 of 6" in summary
    assert "1: " in summary
    assert "5: " in summary
    assert "https://item.example/4" in summary
    assert "https://item.example/5" not in summary
    assert long_title in summary


def test_build_ws_url_maps_http_scheme_and_encodes_params() -> None:
    module = _load_demo_module()

    url = module.build_ws_url(
        "http://127.0.0.1:8000",
        session_id="demo session",
        query="你好",
        user_id="demo_user",
        image_refs=["img1", "img2"],
        video_refs=[],
    )

    assert url.startswith("ws://127.0.0.1:8000/ws/agent/demo%20session?")
    assert "text=%E4%BD%A0%E5%A5%BD" in url
    assert "user_id=demo_user" in url
    assert "client=cli" in url
    assert url.count("image_id=") == 2


def test_build_ws_url_maps_https_and_preserves_base_path() -> None:
    module = _load_demo_module()

    url = module.build_ws_url(
        "https://example.com/api/",
        session_id="s1",
        query="q",
        user_id="u",
        image_refs=[],
        video_refs=["v1", "v2"],
    )

    assert url.startswith("wss://example.com/api/ws/agent/s1?")
    assert url.count("video_id=") == 2


def test_run_client_examples_share_demo_examples(capsys) -> None:
    module = _load_demo_module()

    module.show_examples()
    output = capsys.readouterr().out

    for example in module.get_demo_examples():
        assert example in output


def test_build_ws_url_rejects_non_http_scheme() -> None:
    module = _load_demo_module()

    with pytest.raises(module.RemoteServerError):
        module.build_ws_url(
            "ftp://example.com",
            session_id="s",
            query="q",
            user_id="u",
            image_refs=[],
            video_refs=[],
        )


def test_adapt_remote_response_to_cli_payload_maps_fields() -> None:
    module = _load_demo_module()
    response = {
        "status": "completed",
        "response_text": "done",
        "data": {"summary": "ok"},
        "tool_calls": [
            {"tool_name": "image_generation", "status": "succeeded", "output_ref": "img://1", "error_message": None}
        ],
        "tool_results": [{"tool_name": "image_generation", "success": True}],
        "decision_trace": [{"event": "final_answer"}],
        "errors": [],
        "run_id": "run_1",
        "trace_id": "trace_1",
        "runtime_info": {
            "runtime_profile": "local_demo",
            "graph_mode": "assistant_loop",
            "providers": {"chat": "mock"},
        },
        "current_stage": "final_answer",
        "blocked_reason": None,
    }

    payload = module.adapt_remote_response_to_cli_payload(response, query="hi", events=[])

    assert payload["status"] == "success"
    assert payload["query"] == "hi"
    assert payload["response_data"] == {"summary": "ok"}
    assert payload["tool_sequence"] == ["image_generation"]
    assert payload["tool_calls"][0] == {
        "tool_name": "image_generation",
        "status": "succeeded",
        "output_ref": "img://1",
        "error": None,
    }
    assert payload["provider"] == "mock"
    assert payload["runtime_profile"] == "local_demo"
    assert payload["graph_mode"] == "assistant_loop"
    assert payload["run_id"] == "run_1"
    assert payload["events"] == []


def test_adapt_remote_response_marks_failed_status() -> None:
    module = _load_demo_module()
    response = {
        "status": "failed",
        "response_text": "",
        "errors": [{"code": "TASK_FAILED", "message": "boom", "detail": {}, "recoverable": False}],
    }

    payload = module.adapt_remote_response_to_cli_payload(response, query="hi", events=[])

    assert payload["status"] == "failed"
    assert payload["errors"][0]["message"] == "boom"


def test_adapt_agent_error_to_cli_payload_wraps_string_error() -> None:
    module = _load_demo_module()
    module_event = module.AgentEvent(type="agent_error", session_id="s", run_id="run_9")

    payload = module.adapt_agent_error_to_cli_payload(
        "connection lost", query="hi", events=[module_event]
    )

    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "TASK_FAILED"
    assert payload["errors"][0]["message"] == "connection lost"
    assert payload["run_id"] == "run_9"


def test_run_client_parser_drops_local_provider_flags() -> None:
    module = _load_demo_module()

    args = module.build_parser().parse_args(["--server", "http://localhost:9999", "你好"])

    assert args.server == "http://localhost:9999"
    assert not hasattr(args, "provider")
    assert not hasattr(args, "env_file")


# --------------------------------------------------------------------------- #
# Integration tests against a live server
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    env = {
        **os.environ,
        "MULTIMODAL_AGENT_SKIP_DOTENV": "1",
        "MULTIMODAL_AGENT_DISABLE_DOTENV": "1",
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "multimodal_agent.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_cli(server: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--server", server, *args],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def test_run_client_streams_timeline_against_server(live_server) -> None:
    result = _run_cli(live_server, "生成一张白色运动鞋的电商主图")

    assert result.returncode == 0, result.stderr
    assert "[run] started" in result.stdout
    assert "[plan] image_generation" in result.stdout
    assert "[tool:image_generation] running..." in result.stdout
    assert "[tool:image_generation] succeeded" in result.stdout
    assert "[answer]" in result.stdout
    assert "Run" in result.stdout
    assert "tools: image_generation" in result.stdout
    assert "artifact:" in result.stdout
    assert "event |" not in result.stdout
    assert "trace |" not in result.stdout
    assert "Decision Trace" not in result.stdout
    assert "Authorization" not in result.stdout
    assert "Traceback" not in result.stderr


def test_run_client_debug_events_prints_raw_events(live_server) -> None:
    result = _run_cli(live_server, "--debug-events", "生成一张白色运动鞋的电商主图")

    assert result.returncode == 0, result.stderr
    assert "event | task_started" in result.stdout
    assert "trace | agent_trace_decision" in result.stdout
    assert "event | tool_started" in result.stdout


def test_run_client_show_trace_prints_full_decision_trace(live_server) -> None:
    result = _run_cli(
        live_server,
        "--no-live-events",
        "--show-trace",
        "生成一张白色运动鞋的电商主图",
    )

    assert result.returncode == 0, result.stderr
    assert "Timeline" in result.stdout
    assert "Decision Trace" in result.stdout
    assert "decision_summary:" in result.stdout
    assert "action: image_generation" in result.stdout


def test_run_client_json_mode_does_not_print_live_events(live_server) -> None:
    result = _run_cli(live_server, "--json", "你好")

    assert result.returncode == 0, result.stderr
    assert result.stdout.lstrip().startswith("{")
    assert "event |" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["query"] == "你好"
    assert payload["events"]
    assert "response_data" in payload
    assert "Traceback" not in result.stderr


def test_run_client_saves_replayable_run_log(live_server, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    result = _run_cli(
        live_server,
        "--save-log",
        str(log_dir),
        "生成一张白色运动鞋的电商主图",
    )

    assert result.returncode == 0, result.stderr
    saved_logs = list(log_dir.glob("run_*.json"))
    assert len(saved_logs) == 1
    payload = json.loads(saved_logs[0].read_text(encoding="utf-8"))
    assert payload["demo_metadata"]["request"]["query"] == "生成一张白色运动鞋的电商主图"
    assert payload["demo_metadata"]["server"] == live_server
    assert payload["events"]
    assert "Replay command" in result.stdout


def test_run_client_replays_saved_log(live_server, tmp_path) -> None:
    log_path = tmp_path / "saved.json"
    log_path.write_text(
        json.dumps(
            {
                "query": "旧字段兼容",
                "demo_metadata": {
                    "request": {
                        "query": "你好",
                        "image_refs": [],
                        "video_refs": [],
                        "user_id": "replay_user",
                        "session_id": "replay_session",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        live_server,
        "--no-live-events",
        "--replay-log",
        str(log_path),
    )

    assert result.returncode == 0, result.stderr
    assert "Query" in result.stdout
    assert "你好" in result.stdout
    assert "event |" not in result.stdout
    assert "Timeline" in result.stdout
    assert "[answer]" in result.stdout


def test_run_client_connection_failure_exits_cleanly() -> None:
    unused = f"http://127.0.0.1:{_free_port()}"
    result = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--server", unused, "你好"],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "server_unavailable" in result.stdout
    assert "run_server.py" in result.stdout
    assert "Traceback" not in result.stderr


def test_run_client_connection_failure_json_mode() -> None:
    unused = f"http://127.0.0.1:{_free_port()}"
    result = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--server", unused, "--json", "你好"],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["error"] == "server_unavailable"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_demo_module():
    module_name = "run_client_test"
    spec = importlib.util.spec_from_file_location(module_name, DEMO_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, proc: subprocess.Popen, *, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"Server exited early:\n{output}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Server did not become healthy in time")
