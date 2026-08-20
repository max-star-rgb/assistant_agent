"""Trusted in-image runner for the coding sandbox protocol version 1."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal


@dataclass(frozen=True)
class SandboxRunnerRequest:
    input_root: Path
    workspace_root: Path
    argv: tuple[str, ...]
    kind: Literal["test", "lint", "format", "build"]
    max_output_bytes: int
    max_file_bytes: int
    max_disk_bytes: int
    max_files: int
    max_changed_files: int
    max_patch_bytes: int


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.exceeded = False
        self.lock = threading.Lock()

    def retain(self, chunk: bytes) -> bytes:
        with self.lock:
            remaining = max(0, self.limit - self.total)
            self.total += len(chunk)
            if self.total > self.limit:
                self.exceeded = True
            return chunk[:remaining]


class _StreamCollector(threading.Thread):
    def __init__(self, stream: BinaryIO, budget: _OutputBudget) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.budget = budget
        self.buffer = bytearray()
        self.digest = hashlib.sha256()

    def run(self) -> None:
        while True:
            chunk = self.stream.read(65_536)
            if not chunk:
                return
            self.digest.update(chunk)
            retained = self.budget.retain(chunk)
            if retained:
                self.buffer.extend(retained)


class _RunnerBoundaryError(Exception):
    pass


def run_sandbox_command(request: SandboxRunnerRequest) -> dict[str, object]:
    started = time.monotonic()
    try:
        _prepare_workspace(request)
    except (_RunnerBoundaryError, OSError):
        return _failure("sandbox_resource_exceeded", started)

    budget = _OutputBudget(request.max_output_bytes)
    try:
        process = subprocess.Popen(
            request.argv,
            cwd=request.workspace_root,
            env=_command_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError:
        return _failure("sandbox_start_failed", started)
    if process.stdout is None or process.stderr is None:
        _terminate(process)
        return _failure("sandbox_start_failed", started)

    stdout_collector = _StreamCollector(process.stdout, budget)
    stderr_collector = _StreamCollector(process.stderr, budget)
    stdout_collector.start()
    stderr_collector.start()
    while process.poll() is None:
        if budget.exceeded:
            _terminate(process)
            break
        time.sleep(0.01)
    return_code = process.wait()
    stdout_collector.join(timeout=5)
    stderr_collector.join(timeout=5)
    stdout = bytes(stdout_collector.buffer).decode("utf-8", errors="replace")
    stderr = bytes(stderr_collector.buffer).decode("utf-8", errors="replace")
    output_digest = hashlib.sha256(
        stdout_collector.digest.digest() + b"\x00" + stderr_collector.digest.digest()
    ).hexdigest()
    if budget.exceeded:
        return _result(
            "resource_exceeded",
            started,
            exit_code=return_code,
            output_digest=output_digest,
            stdout=stdout,
            stderr=stderr,
            truncated=True,
            error_code="sandbox_resource_exceeded",
        )
    if return_code != 0:
        return _result(
            "failed",
            started,
            exit_code=return_code,
            output_digest=output_digest,
            stdout=stdout,
            stderr=stderr,
            error_code="verification_command_failed",
        )

    formatter_files: dict[str, str] = {}
    formatter_deletions: tuple[str, ...] = ()
    formatter_modes: dict[str, int] = {}
    if request.kind == "format":
        try:
            formatter_files, formatter_deletions, formatter_modes = _formatter_changes(
                request
            )
        except _RunnerBoundaryError:
            return _result(
                "resource_exceeded",
                started,
                exit_code=return_code,
                output_digest=output_digest,
                stdout=stdout,
                stderr=stderr,
                error_code="sandbox_resource_exceeded",
            )
    return _result(
        "passed",
        started,
        exit_code=return_code,
        output_digest=output_digest,
        stdout=stdout,
        stderr=stderr,
        formatter_files=formatter_files,
        formatter_deletions=formatter_deletions,
        formatter_modes=formatter_modes,
    )


def _prepare_workspace(request: SandboxRunnerRequest) -> None:
    if not request.input_root.is_dir() or not request.workspace_root.is_dir():
        raise _RunnerBoundaryError
    if any(request.workspace_root.iterdir()):
        raise _RunnerBoundaryError
    count = 0
    total = 0
    for source, is_directory in _walk_entries(request.input_root):
        relative = source.relative_to(request.input_root)
        count += 1
        if count > request.max_files:
            raise _RunnerBoundaryError
        destination = request.workspace_root / relative
        if is_directory:
            destination.mkdir()
            continue
        size = source.stat().st_size
        total += size
        if (
            size > request.max_file_bytes
            or total > request.max_disk_bytes
        ):
            raise _RunnerBoundaryError
        shutil.copy2(source, destination, follow_symlinks=False)


def _walk_entries(root: Path):
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise _RunnerBoundaryError from exc
        child_directories: list[Path] = []
        for entry in entries:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise _RunnerBoundaryError from exc
            path = Path(entry.path)
            if stat.S_ISDIR(mode):
                yield path, True
                child_directories.append(path)
            elif stat.S_ISREG(mode):
                yield path, False
            else:
                raise _RunnerBoundaryError
        stack.extend(reversed(child_directories))


def _walk_regular(root: Path):
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise _RunnerBoundaryError from exc
        for entry in entries:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise _RunnerBoundaryError from exc
            if stat.S_ISDIR(mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(mode):
                yield Path(entry.path)
            else:
                raise _RunnerBoundaryError


def _formatter_changes(
    request: SandboxRunnerRequest,
) -> tuple[dict[str, str], tuple[str, ...], dict[str, int]]:
    input_files = {
        path.relative_to(request.input_root).as_posix(): path
        for path in _walk_regular(request.input_root)
        if ".git" not in path.relative_to(request.input_root).parts
    }
    workspace_files = {
        path.relative_to(request.workspace_root).as_posix(): path
        for path in _walk_regular(request.workspace_root)
        if ".git" not in path.relative_to(request.workspace_root).parts
    }
    changed: dict[str, str] = {}
    deletions = tuple(sorted(set(input_files).difference(workspace_files)))
    modes: dict[str, int] = {}
    payload_bytes = 0
    for relative, workspace_path in sorted(workspace_files.items()):
        workspace_bytes = _read_bounded(workspace_path, request.max_file_bytes)
        input_path = input_files.get(relative)
        input_bytes = (
            _read_bounded(input_path, request.max_file_bytes)
            if input_path is not None
            else None
        )
        workspace_mode = stat.S_IMODE(workspace_path.stat().st_mode)
        input_mode = (
            stat.S_IMODE(input_path.stat().st_mode) if input_path is not None else None
        )
        if workspace_mode & ~0o777:
            raise _RunnerBoundaryError
        if workspace_mode != input_mode:
            modes[relative] = workspace_mode
        if workspace_bytes == input_bytes:
            continue
        try:
            content = workspace_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _RunnerBoundaryError from exc
        payload_bytes += len(relative.encode()) + len(workspace_bytes)
        if (
            len(changed) >= request.max_changed_files
            or payload_bytes > request.max_patch_bytes
        ):
            raise _RunnerBoundaryError
        changed[relative] = content
    changed_paths = set(changed) | set(deletions) | set(modes)
    payload_bytes += sum(len(path.encode()) for path in deletions)
    payload_bytes += sum(len(path.encode()) + 4 for path in modes)
    if (
        len(changed_paths) > request.max_changed_files
        or payload_bytes > request.max_patch_bytes
    ):
        raise _RunnerBoundaryError
    return changed, deletions, modes


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise _RunnerBoundaryError
        return path.read_bytes()
    except OSError as exc:
        raise _RunnerBoundaryError from exc


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass


def _command_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/home/sandbox",
        "TMPDIR": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _failure(error_code: str, started: float) -> dict[str, object]:
    return _result(
        "resource_exceeded" if error_code == "sandbox_resource_exceeded" else "failed",
        started,
        exit_code=None,
        output_digest=hashlib.sha256(b"").hexdigest(),
        error_code=error_code,
    )


def _result(
    status: str,
    started: float,
    *,
    exit_code: int | None,
    output_digest: str,
    stdout: str = "",
    stderr: str = "",
    truncated: bool = False,
    error_code: str | None = None,
    formatter_files: dict[str, str] | None = None,
    formatter_deletions: tuple[str, ...] = (),
    formatter_modes: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "status": status,
        "exit_code": exit_code,
        "duration_ms": max(0, int((time.monotonic() - started) * 1_000)),
        "output_digest": output_digest,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "error_code": error_code,
        "formatter_files": formatter_files or {},
        "formatter_deletions": formatter_deletions,
        "formatter_modes": formatter_modes or {},
    }


def _parse_args() -> SandboxRunnerRequest:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("test", "lint", "format", "build"))
    parser.add_argument("--max-output-bytes", required=True, type=int)
    parser.add_argument("--max-file-bytes", required=True, type=int)
    parser.add_argument("--max-disk-bytes", required=True, type=int)
    parser.add_argument("--max-files", required=True, type=int)
    parser.add_argument("--max-changed-files", required=True, type=int)
    parser.add_argument("--max-patch-bytes", required=True, type=int)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    values = parser.parse_args()
    argv = tuple(values.argv[1:] if values.argv[:1] == ["--"] else values.argv)
    if not argv:
        parser.error("command argv is required")
    return SandboxRunnerRequest(
        input_root=Path("/input"),
        workspace_root=Path("/workspace"),
        argv=argv,
        kind=values.kind,
        max_output_bytes=values.max_output_bytes,
        max_file_bytes=values.max_file_bytes,
        max_disk_bytes=values.max_disk_bytes,
        max_files=values.max_files,
        max_changed_files=values.max_changed_files,
        max_patch_bytes=values.max_patch_bytes,
    )


def main() -> int:
    _make_protocol_private()
    result = run_sandbox_command(_parse_args())
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    os.write(1, payload)
    return 0


def _make_protocol_private() -> None:
    pr_set_dumpable = 4
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(pr_set_dumpable, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not protect runner protocol fd")


if __name__ == "__main__":
    raise SystemExit(main())
