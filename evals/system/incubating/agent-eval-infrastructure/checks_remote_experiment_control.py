from __future__ import annotations

import json
import signal
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from assistant_agent.evaluation import remote_run_control


TRIGGER_ID = "abc123"
PID = 43210
COMMAND = [
    "/opt/hello_agent/bin/python",
    "/workspace/scripts/run_agent_evals.py",
    "--run",
    "--dataset-active",
]


def test_status_derives_live_task_progress_from_receipt_and_events(
    tmp_path: Path,
) -> None:
    _write_receipt(tmp_path, status="accepted")
    _write_events(
        tmp_path,
        [
            {"event": "agent_eval.run.started", "task_count": 3},
            {
                "event": "agent_eval.task.started",
                "task_id": "email_empty_result_honesty",
            },
            {
                "event": "agent_eval.evaluation.completed",
                "task_id": "email_empty_result_honesty",
            },
            {
                "event": "agent_eval.task.started",
                "task_id": "memory_current_request_precedence",
            },
            {
                "event": "agent_eval.judge.started",
                "task_id": "memory_current_request_precedence",
                "criterion_id": "grounding",
            },
        ],
    )

    status = remote_run_control.read_remote_run_status(
        tmp_path,
        process_alive=lambda pid: pid == PID,
    )

    assert status.model_dump(mode="json") == {
        "trigger_id": TRIGGER_ID,
        "run_name": "ui-release",
        "status": "running",
        "pid": PID,
        "task_count": 3,
        "completed_task_count": 1,
        "current_task_id": "memory_current_request_precedence",
        "current_stage": "judge:grounding",
        "last_event": "agent_eval.judge.started",
        "created_at": "2026-07-29T12:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "stop_requested_at": None,
    }


def test_status_selects_latest_receipt_by_created_at(tmp_path: Path) -> None:
    _write_receipt(
        tmp_path,
        trigger_id="older",
        run_name="older-run",
        created_at="2026-07-29T11:59:59Z",
        status="completed",
        pid=40001,
    )
    _write_receipt(
        tmp_path,
        trigger_id="newer",
        run_name="newer-run",
        created_at="2026-07-29T12:00:01Z",
        status="failed",
        pid=40002,
    )

    status = remote_run_control.read_remote_run_status(tmp_path)

    assert status.trigger_id == "newer"
    assert status.run_name == "newer-run"
    assert status.status == "failed"


def test_stop_marks_receipt_before_terminating_verified_process_group(
    tmp_path: Path,
) -> None:
    receipt_path = _write_receipt(tmp_path, status="accepted")
    calls: list[tuple[int, signal.Signals]] = []

    status = remote_run_control.request_remote_run_stop(
        tmp_path,
        process_cmdline=lambda pid: COMMAND if pid == PID else [],
        process_group_id=lambda pid: pid,
        terminate_process_group=lambda pgid, sig: calls.append((pgid, sig)),
        now=lambda: datetime(2026, 7, 29, 12, 5, tzinfo=UTC),
    )

    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "stop_requested"
    assert persisted["stop_requested_at"] == "2026-07-29T12:05:00Z"
    assert calls == [(PID, signal.SIGTERM)]
    assert status.status == "stop_requested"


def test_stop_refuses_pid_reuse_without_signalling(tmp_path: Path) -> None:
    receipt_path = _write_receipt(tmp_path, status="accepted")
    before = receipt_path.read_text(encoding="utf-8")
    calls: list[tuple[int, signal.Signals]] = []

    with pytest.raises(
        remote_run_control.RemoteRunControlError,
        match="does not match",
    ):
        remote_run_control.request_remote_run_stop(
            tmp_path,
            process_cmdline=lambda _pid: ["/usr/bin/python", "another.py"],
            process_group_id=lambda pid: pid,
            terminate_process_group=lambda pgid, sig: calls.append((pgid, sig)),
        )

    assert receipt_path.read_text(encoding="utf-8") == before
    assert calls == []


def test_server_console_reads_status_command_and_exits_on_stdin_eof(
    tmp_path: Path,
) -> None:
    _write_receipt(
        tmp_path,
        status="completed",
        finished_at="2026-07-29T12:01:00Z",
        exit_code=0,
    )
    _write_events(
        tmp_path,
        [
            {"event": "agent_eval.run.started", "task_count": 1},
            {
                "event": "agent_eval.task.started",
                "task_id": "email_empty_result_honesty",
            },
            {
                "event": "agent_eval.evaluation.completed",
                "task_id": "email_empty_result_honesty",
            },
        ],
    )
    output = StringIO()
    error = StringIO()

    thread = remote_run_control.start_remote_eval_console(
        artifact_root=tmp_path,
        input_stream=StringIO("eval status\n"),
        output_stream=output,
        error_stream=error,
    )
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert "1/1" in output.getvalue()
    assert 'run_name="ui-release"' in output.getvalue()
    assert "task=" not in output.getvalue()
    assert "completed" in output.getvalue()
    assert "eval status [trigger_id] | eval stop [trigger_id]" not in output.getvalue()
    assert error.getvalue() == ""


def test_server_console_routes_stop_to_selected_trigger() -> None:
    selected: list[str | None] = []
    stopped = remote_run_control.RemoteExperimentRunStatus(
        trigger_id=TRIGGER_ID,
        run_name="ui-release",
        status="stop_requested",
        pid=PID,
        created_at="2026-07-29T12:00:00Z",
    )

    result = remote_run_control.execute_remote_eval_console_command(
        f"eval stop {TRIGGER_ID}",
        stop_requester=lambda _root, trigger_id: (
            selected.append(trigger_id) or stopped
        ),
    )

    assert selected == [TRIGGER_ID]
    assert "stop_requested" in result


def _write_receipt(
    artifact_root: Path,
    *,
    trigger_id: str = TRIGGER_ID,
    run_name: str = "ui-release",
    created_at: str = "2026-07-29T12:00:00Z",
    status: str,
    pid: int = PID,
    finished_at: str | None = None,
    exit_code: int | None = None,
) -> Path:
    receipt = {
        "trigger_id": trigger_id,
        "run_name": run_name,
        "status": status,
        "pid": pid,
        "command": COMMAND,
        "created_at": created_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "stdout_log": str(artifact_root / f"{trigger_id}.stdout.log"),
        "stderr_log": str(artifact_root / f"{trigger_id}.stderr.log"),
    }
    path = artifact_root / f"{trigger_id}.json"
    path.write_text(
        json.dumps(receipt, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _write_events(artifact_root: Path, events: list[dict[str, object]]) -> None:
    path = artifact_root / f"{TRIGGER_ID}.stderr.log"
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
