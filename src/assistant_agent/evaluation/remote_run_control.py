"""Inspect and stop Langfuse-triggered Agent eval runs from the server console."""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from io import TextIOBase
from pathlib import Path
from threading import Thread
from typing import Any, Literal

from pydantic import BaseModel


DEFAULT_REMOTE_RUN_ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[3] / ".data" / "evals" / "remote"
)
TERMINAL_REMOTE_RUN_STATUSES = frozenset({"completed", "failed", "stopped"})
RemoteRunStatusName = Literal[
    "accepted",
    "running",
    "stop_requested",
    "stopped",
    "completed",
    "failed",
    "orphaned",
]

ProcessAlive = Callable[[int], bool]
ProcessCmdline = Callable[[int], Sequence[str]]
ProcessGroupId = Callable[[int], int]
TerminateProcessGroup = Callable[[int, signal.Signals], None]
StatusReader = Callable[[Path, str | None], "RemoteExperimentRunStatus"]
StopRequester = Callable[[Path, str | None], "RemoteExperimentRunStatus"]


class RemoteRunControlError(RuntimeError):
    """The requested remote-run control operation is unsafe or invalid."""


class RemoteRunNotFound(RemoteRunControlError):
    pass


class RemoteExperimentRunStatus(BaseModel):
    trigger_id: str
    run_name: str
    status: RemoteRunStatusName
    pid: int
    task_count: int | None = None
    completed_task_count: int = 0
    current_task_id: str | None = None
    current_stage: str | None = None
    last_event: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    stop_requested_at: datetime | None = None


class RemoteProgressTracker:
    """Project JSON progress events into one operator-facing status."""

    def __init__(self) -> None:
        self.task_count: int | None = None
        self.completed_task_ids: set[str] = set()
        self.current_task_id: str | None = None
        self.current_stage: str | None = None
        self.last_event: str | None = None

    def consume(self, event: dict[str, Any]) -> str | None:
        event_name = event.get("event")
        if not isinstance(event_name, str):
            return None
        self.last_event = event_name
        task_count = event.get("task_count")
        if (
            event_name == "agent_eval.run.started"
            and isinstance(task_count, int)
            and not isinstance(task_count, bool)
            and task_count >= 0
        ):
            self.task_count = task_count
            self.current_stage = "starting"
        task_id = event.get("task_id")
        if isinstance(task_id, str) and task_id:
            self.current_task_id = task_id
        if event_name == "agent_eval.task.started":
            self.current_stage = "agent"
        elif event_name == "agent_eval.task.completed":
            self.current_stage = "agent_completed"
        elif event_name == "agent_eval.evaluation.started":
            self.current_stage = "evaluation"
        elif event_name == "agent_eval.judge.started":
            criterion_id = event.get("criterion_id")
            self.current_stage = (
                f"judge:{criterion_id}"
                if isinstance(criterion_id, str) and criterion_id
                else "judge"
            )
        elif event_name == "agent_eval.judge.completed":
            criterion_id = event.get("criterion_id")
            self.current_stage = (
                f"judge:{criterion_id}:completed"
                if isinstance(criterion_id, str) and criterion_id
                else "judge_completed"
            )
        elif event_name == "agent_eval.evaluation.completed":
            if isinstance(task_id, str) and task_id:
                self.completed_task_ids.add(task_id)
            self.current_stage = "task_completed"
        elif event_name == "agent_eval.run.completed":
            self.current_stage = "finalizing"
        elif event_name.endswith(".failed"):
            self.current_stage = "failed"
        return format_progress_event(self, event_name)


def read_remote_run_status(
    artifact_root: Path = DEFAULT_REMOTE_RUN_ARTIFACT_ROOT,
    *,
    trigger_id: str | None = None,
    process_alive: ProcessAlive | None = None,
) -> RemoteExperimentRunStatus:
    receipt_path, receipt = _load_receipt(artifact_root, trigger_id=trigger_id)
    tracker = RemoteProgressTracker()
    for event in iter_progress_events(_stderr_log_path(receipt_path, receipt)):
        tracker.consume(event)

    persisted_status = receipt.get("status")
    pid = _required_positive_int(receipt, "pid")
    if persisted_status in TERMINAL_REMOTE_RUN_STATUSES:
        status = str(persisted_status)
    elif persisted_status == "stop_requested":
        status = "stop_requested"
    else:
        is_alive = (process_alive or _process_alive)(pid)
        status = "running" if is_alive else "orphaned"

    return RemoteExperimentRunStatus(
        trigger_id=_required_string(receipt, "trigger_id"),
        run_name=_required_string(receipt, "run_name"),
        status=status,
        pid=pid,
        task_count=tracker.task_count,
        completed_task_count=len(tracker.completed_task_ids),
        current_task_id=tracker.current_task_id,
        current_stage=tracker.current_stage,
        last_event=tracker.last_event,
        created_at=_required_string(receipt, "created_at"),
        finished_at=receipt.get("finished_at"),
        exit_code=receipt.get("exit_code"),
        stop_requested_at=receipt.get("stop_requested_at"),
    )


def request_remote_run_stop(
    artifact_root: Path = DEFAULT_REMOTE_RUN_ARTIFACT_ROOT,
    *,
    trigger_id: str | None = None,
    process_cmdline: ProcessCmdline | None = None,
    process_group_id: ProcessGroupId | None = None,
    terminate_process_group: TerminateProcessGroup | None = None,
    now: Callable[[], datetime] | None = None,
) -> RemoteExperimentRunStatus:
    receipt_path, receipt = _load_receipt(artifact_root, trigger_id=trigger_id)
    status = receipt.get("status")
    if status in TERMINAL_REMOTE_RUN_STATUSES:
        raise RemoteRunControlError(
            f"Remote Agent eval run is already {status}: "
            f"{receipt.get('trigger_id')}."
        )
    if status == "stop_requested":
        return read_remote_run_status(artifact_root, trigger_id=trigger_id)

    pid = _required_positive_int(receipt, "pid")
    expected_command = receipt.get("command")
    if not (
        isinstance(expected_command, list)
        and expected_command
        and all(isinstance(part, str) and part for part in expected_command)
    ):
        raise RemoteRunControlError(
            "Remote Agent eval receipt has no verifiable launch command; "
            "refusing to signal the process."
        )
    actual_command = list((process_cmdline or _process_cmdline)(pid))
    if actual_command != expected_command:
        raise RemoteRunControlError(
            f"Process {pid} command does not match the recorded Agent eval run; "
            "refusing to signal it."
        )
    pgid = (process_group_id or os.getpgid)(pid)
    if pgid != pid:
        raise RemoteRunControlError(
            f"Process {pid} is not the recorded isolated process-group leader; "
            "refusing to signal it."
        )

    stop_time = (now or _utc_now)()
    receipt["status"] = "stop_requested"
    receipt["stop_requested_at"] = _isoformat_utc(stop_time)
    _write_json_atomic(receipt_path, receipt)
    terminate = terminate_process_group or _terminate_process_group
    try:
        terminate(pgid, signal.SIGTERM)
    except ProcessLookupError:
        receipt["status"] = "stopped"
        receipt["finished_at"] = _isoformat_utc(stop_time)
        _write_json_atomic(receipt_path, receipt)
    return read_remote_run_status(
        artifact_root,
        trigger_id=_required_string(receipt, "trigger_id"),
        process_alive=lambda _pid: True,
    )


def iter_progress_events(path: Path, *, offset: int = 0) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    yield payload
    except FileNotFoundError:
        return


def format_progress_event(
    tracker: RemoteProgressTracker,
    event_name: str,
) -> str | None:
    if not event_name.startswith("agent_eval."):
        return None
    completed = len(tracker.completed_task_ids)
    total = "?" if tracker.task_count is None else str(tracker.task_count)
    task = (
        f" task={tracker.current_task_id}"
        if tracker.current_task_id is not None
        else ""
    )
    stage = (
        f" stage={tracker.current_stage}"
        if tracker.current_stage is not None
        else ""
    )
    return f"{completed}/{total}{task}{stage}"


def format_remote_run_status(status: RemoteExperimentRunStatus) -> str:
    total = "?" if status.task_count is None else str(status.task_count)
    run_name = json.dumps(status.run_name, ensure_ascii=False)
    stage = f" stage={status.current_stage}" if status.current_stage else ""
    return (
        f"[agent-eval {status.trigger_id[:8]}] {status.status} "
        f'{status.completed_task_count}/{total} run_name={run_name}{stage}'
    )


def execute_remote_eval_console_command(
    line: str,
    *,
    artifact_root: Path = DEFAULT_REMOTE_RUN_ARTIFACT_ROOT,
    status_reader: StatusReader | None = None,
    stop_requester: StopRequester | None = None,
) -> str:
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise RemoteRunControlError(f"Invalid eval console command: {exc}.") from exc
    if parts in (["eval", "help"], ["eval", "--help"]):
        return _remote_eval_console_usage()
    if (
        len(parts) not in {2, 3}
        or not parts
        or parts[0] != "eval"
        or parts[1] not in {"status", "stop"}
    ):
        raise RemoteRunControlError(_remote_eval_console_usage())
    trigger_id = parts[2] if len(parts) == 3 else None
    if parts[1] == "status":
        reader = status_reader or _read_status_for_console
        status = reader(artifact_root, trigger_id)
    else:
        requester = stop_requester or _request_stop_for_console
        status = requester(artifact_root, trigger_id)
    return format_remote_run_status(status)


def start_remote_eval_console(
    *,
    artifact_root: Path = DEFAULT_REMOTE_RUN_ARTIFACT_ROOT,
    input_stream: TextIOBase | None = None,
    output_stream: TextIOBase | None = None,
    error_stream: TextIOBase | None = None,
) -> Thread:
    thread = Thread(
        target=_run_remote_eval_console,
        kwargs={
            "artifact_root": artifact_root,
            "input_stream": input_stream or sys.stdin,
            "output_stream": output_stream or sys.stdout,
            "error_stream": error_stream or sys.stderr,
        },
        name="agent-eval-console",
        daemon=True,
    )
    thread.start()
    return thread


def _run_remote_eval_console(
    *,
    artifact_root: Path,
    input_stream: TextIOBase,
    output_stream: TextIOBase,
    error_stream: TextIOBase,
) -> None:
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            result = execute_remote_eval_console_command(
                line,
                artifact_root=artifact_root,
            )
        except RemoteRunControlError as exc:
            print(
                f"[agent-eval] control error: {exc}",
                file=error_stream,
                flush=True,
            )
        else:
            print(result, file=output_stream, flush=True)


def _read_status_for_console(
    artifact_root: Path,
    trigger_id: str | None,
) -> RemoteExperimentRunStatus:
    return read_remote_run_status(
        artifact_root,
        trigger_id=trigger_id,
    )


def _request_stop_for_console(
    artifact_root: Path,
    trigger_id: str | None,
) -> RemoteExperimentRunStatus:
    return request_remote_run_stop(
        artifact_root,
        trigger_id=trigger_id,
    )


def _remote_eval_console_usage() -> str:
    return "eval status [trigger_id] | eval stop [trigger_id]"


def _load_receipt(
    artifact_root: Path,
    *,
    trigger_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    if trigger_id is not None:
        candidates = [artifact_root / f"{trigger_id}.json"]
    else:
        candidates = list(artifact_root.glob("*.json"))
    receipts: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        created_at = payload.get("created_at")
        if not isinstance(created_at, str):
            continue
        try:
            parsed_created_at = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        receipts.append((parsed_created_at, path, payload))
    if not receipts:
        target = trigger_id or "latest"
        raise RemoteRunNotFound(f"Remote Agent eval run not found: {target}.")
    _, path, receipt = max(receipts, key=lambda item: item[0])
    return path, receipt


def _stderr_log_path(receipt_path: Path, receipt: dict[str, Any]) -> Path:
    configured = receipt.get("stderr_log")
    if isinstance(configured, str) and configured:
        return Path(configured)
    return receipt_path.with_suffix(".stderr.log")


def _required_string(receipt: dict[str, Any], field: str) -> str:
    value = receipt.get(field)
    if not isinstance(value, str) or not value:
        raise RemoteRunControlError(
            f"Remote Agent eval receipt has invalid {field}."
        )
    return value


def _required_positive_int(receipt: dict[str, Any], field: str) -> int:
    value = receipt.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 1:
        raise RemoteRunControlError(
            f"Remote Agent eval receipt has invalid {field}."
        )
    return value


def _process_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").is_dir()


def _process_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise RemoteRunControlError(
            f"Cannot inspect remote Agent eval process {pid}."
        ) from exc
    return [
        part.decode("utf-8", errors="surrogateescape")
        for part in raw.split(b"\0")
        if part
    ]


def _terminate_process_group(pgid: int, sig: signal.Signals) -> None:
    os.killpg(pgid, sig)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
