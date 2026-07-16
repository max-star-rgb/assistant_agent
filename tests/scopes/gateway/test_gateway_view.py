import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = "scripts/gateway_view.py"


def test_gateway_view_tail_renders_readable_timeline(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.log"
    log_path.write_text(
        "\n".join(
            [
                (
                    "2026-07-16T12:00:00.000Z level=INFO component=gateway "
                    "event=gateway.server.starting run_id=- turn_id=- trace_id=- "
                    "server_starting host=127.0.0.1 port=8089 log_dir=.data/logs"
                ),
                (
                    "2026-07-16T12:00:01.000Z level=INFO component=gateway "
                    "event=gateway.session.acquired run_id=- turn_id=- trace_id=- "
                    "user_id=sha256:user session_id=sha256:session created=True"
                ),
                (
                    "2026-07-16T12:00:02.000Z level=INFO component=gateway "
                    "event=gateway.run.started run_id=run_gateway_123456 "
                    "turn_id=turn_gateway_abcdef trace_id=trace_runtime_999999 "
                    "user_id=sha256:user session_id=sha256:session queue_depth=2"
                ),
                (
                    "2026-07-16T12:00:03.000Z level=INFO component=gateway "
                    "event=gateway.run.completed run_id=run_gateway_123456 "
                    "turn_id=turn_gateway_abcdef trace_id=trace_runtime_999999 "
                    "user_id=sha256:user session_id=sha256:session status=completed"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_gateway_view("--log-path", str(log_path), "--tail", "10")

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
    assert "gateway.run.completed" in result.stdout


def test_gateway_view_follow_limit_prints_appended_event(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.log"
    log_path.write_text("", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            SCRIPT_PATH,
            "--log-path",
            str(log_path),
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
    log_path.write_text(
        (
            "2026-07-16T12:01:00.000Z level=INFO component=gateway "
            "event=gateway.run.cancel_requested run_id=run_cancel "
            "turn_id=turn_cancel trace_id=trace_cancel "
            "user_id=sha256:user session_id=sha256:session source=client phase=active\n"
        ),
        encoding="utf-8",
    )

    remaining_stdout, stderr = process.communicate(timeout=10)
    stdout = header + remaining_stdout

    assert process.returncode == 0, stderr
    assert "gateway.run.cancel_requested" in stdout
    assert "cancel requested source=client phase=active" in stdout
    assert "run=run_cancel" in stdout


def test_gateway_view_bad_line_falls_back_to_raw_summary(tmp_path: Path) -> None:
    log_path = tmp_path / "gateway.log"
    log_path.write_text("this is not gateway key value output\n", encoding="utf-8")

    result = _run_gateway_view("--log-path", str(log_path), "--tail", "5")

    assert result.returncode == 0, result.stderr
    assert "raw log line" in result.stdout
    assert "this is not gateway key value output" in result.stdout


def _run_gateway_view(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        check=False,
        capture_output=True,
        text=True,
    )
