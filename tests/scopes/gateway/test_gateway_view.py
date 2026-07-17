import subprocess
import sys
import json
from pathlib import Path


SCRIPT_PATH = "scripts/gateway_view.py"


def test_gateway_lifecycle_jsonl_store_persists_prompt_safe_record(tmp_path: Path) -> None:
    from assistant_agent.gateway.observability import (
        GatewayLifecycleEvent,
        JsonlGatewayLifecycleStore,
    )

    event_path = tmp_path / "gateway_events.jsonl"
    store = JsonlGatewayLifecycleStore(event_path)
    store.append(
        GatewayLifecycleEvent(
            type="gateway.run.cancel_requested",
            user_id="user-secret-123",
            session_id="session-secret-456",
            run_id="run_gateway_123456",
            turn_id="turn_gateway_abcdef",
            payload={
                "trace_id": "trace_runtime_999999",
                "reason": "client supplied text with sk-secret",
                "source": "client",
                "queue_depth": 2,
                "unsafe_payload": "do not persist",
            },
        )
    )

    [record] = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["schema_version"] == "gateway_lifecycle_event_v1"
    assert record["component"] == "gateway"
    assert record["event"] == "gateway.run.cancel_requested"
    assert record["run_id"] == "run_gateway_123456"
    assert record["turn_id"] == "turn_gateway_abcdef"
    assert record["trace_id"] == "trace_runtime_999999"
    assert record["user_id"].startswith("sha256:")
    assert record["session_id"].startswith("sha256:")
    assert "user-secret-123" not in json.dumps(record)
    assert "session-secret-456" not in json.dumps(record)
    assert record["attributes"] == {
        "queue_depth": 2,
        "reason": "client_supplied",
        "source": "client",
    }


def test_gateway_view_tail_renders_jsonl_timeline(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    _write_jsonl(
        event_path,
        [
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:00.000Z",
                "component": "gateway",
                "event": "gateway.server.starting",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": None,
                "session_id": None,
                "attributes": {"host": "127.0.0.1", "port": 8089},
            },
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:01.000Z",
                "component": "gateway",
                "event": "gateway.session.acquired",
                "run_id": None,
                "turn_id": None,
                "trace_id": None,
                "user_id": "sha256:user",
                "session_id": "sha256:session",
                "attributes": {"created": True},
            },
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:00:02.000Z",
                "component": "gateway",
                "event": "gateway.run.started",
                "run_id": "run_gateway_123456",
                "turn_id": "turn_gateway_abcdef",
                "trace_id": "trace_runtime_999999",
                "user_id": "sha256:user",
                "session_id": "sha256:session",
                "attributes": {"queue_depth": 2},
            },
        ],
    )

    result = _run_gateway_view("--event-path", str(event_path), "--tail", "10")

    assert result.returncode == 0, result.stderr
    assert "Gateway timeline" in result.stdout
    assert "gateway.server.starting" in result.stdout
    assert "server starting host=127.0.0.1 port=8089" in result.stdout
    assert "gateway.session.acquired" in result.stdout
    assert "session acquired created=True" in result.stdout
    assert "gateway.run.started" in result.stdout
    assert "run=run_gateway_123456" in result.stdout
    assert "turn=turn_gateway_abcdef" in result.stdout
    assert "trace=trace_runtime_999999" in result.stdout
    assert "queue_depth=2" in result.stdout


def test_gateway_view_follow_limit_prints_appended_jsonl_event(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    event_path.write_text("", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            SCRIPT_PATH,
            "--event-path",
            str(event_path),
            "--tail",
            "0",
            "--follow",
            "--poll-interval",
            "0.05",
            "--follow-limit",
            "1",
            "--follow-timeout",
            "5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    header = process.stdout.readline()
    assert "Gateway timeline" in header
    _write_jsonl(
        event_path,
        [
            {
                "schema_version": "gateway_lifecycle_event_v1",
                "created_at": "2026-07-16T12:01:00.000Z",
                "component": "gateway",
                "event": "gateway.run.cancel_requested",
                "run_id": "run_cancel",
                "turn_id": "turn_cancel",
                "trace_id": "trace_cancel",
                "user_id": "sha256:user",
                "session_id": "sha256:session",
                "attributes": {"source": "client", "phase": "active"},
            }
        ],
    )

    remaining_stdout, stderr = process.communicate(timeout=10)
    stdout = header + remaining_stdout

    assert process.returncode == 0, stderr
    assert "gateway.run.cancel_requested" in stdout
    assert "cancel requested source=client phase=active" in stdout
    assert "run=run_cancel" in stdout


def test_gateway_view_bad_jsonl_line_falls_back_to_raw_summary(tmp_path: Path) -> None:
    event_path = tmp_path / "gateway_events.jsonl"
    event_path.write_text("this is not gateway jsonl output\n", encoding="utf-8")

    result = _run_gateway_view("--event-path", str(event_path), "--tail", "5")

    assert result.returncode == 0, result.stderr
    assert "raw log line" in result.stdout
    assert "this is not gateway jsonl output" in result.stdout


def test_gateway_view_legacy_log_path_still_renders_key_value_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.log"
    log_path.write_text(
        (
            "2026-07-16T12:01:00.000Z level=INFO component=gateway "
            "event=gateway.run.cancel_requested run_id=run_cancel "
            "turn_id=turn_cancel trace_id=trace_cancel "
            "user_id=sha256:user session_id=sha256:session source=client phase=active\n"
        ),
        encoding="utf-8",
    )

    result = _run_gateway_view("--log-path", str(log_path), "--tail", "5")

    assert result.returncode == 0, result.stderr
    assert "gateway.run.cancel_requested" in result.stdout
    assert "cancel requested source=client phase=active" in result.stdout


def _run_gateway_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
