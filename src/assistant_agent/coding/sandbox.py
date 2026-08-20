"""Fail-closed container execution for trusted coding validation commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from assistant_agent.coding.models import CodingSandboxRequest, CodingSandboxResult


_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_OWNER_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")


class CodingSandboxBackend(Protocol):
    """Trusted execution boundary consumed by the validation service."""

    def execute(self, request: CodingSandboxRequest) -> CodingSandboxResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


class DockerCommandRunner(Protocol):
    """Narrow injectable seam around the trusted local Docker CLI."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def popen(
        self,
        argv: tuple[str, ...],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> subprocess.Popen[bytes]:
        raise NotImplementedError


class SubprocessDockerCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def popen(
        self,
        argv: tuple[str, ...],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            shell=False,
        )


class DockerCodingSandboxBackend:
    """Execute fixed validation commands in short-lived Docker containers."""

    def __init__(
        self,
        docker_binary: str = "docker",
        *,
        command_runner: DockerCommandRunner | None = None,
        owner_id: str | None = None,
        uid: int | None = None,
        gid: int | None = None,
        poll_interval_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved_owner = owner_id or uuid.uuid4().hex
        if _OWNER_ID.fullmatch(resolved_owner) is None:
            raise ValueError("coding sandbox owner id is invalid")
        if not docker_binary or any(item in docker_binary for item in ("\x00", "\n", "\r")):
            raise ValueError("coding sandbox Docker binary is invalid")
        self._docker = docker_binary
        self._runner = command_runner or SubprocessDockerCommandRunner()
        self._owner_id = resolved_owner
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._poll_interval_seconds = max(0.01, poll_interval_seconds)
        self._monotonic = monotonic

    def execute(self, request: CodingSandboxRequest) -> CodingSandboxResult:
        started = self._monotonic()
        if self._uid <= 0 or self._gid < 0:
            return self._failure(
                "sandbox_user_invalid",
                started=started,
                cleanup_status="not_created",
            )
        if not request.scratch_root.is_absolute() or not request.scratch_root.is_dir():
            return self._failure(
                "sandbox_workspace_invalid",
                started=started,
                cleanup_status="not_created",
            )
        scratch_value = str(request.scratch_root)
        if any(item in scratch_value for item in (",", "\x00", "\n", "\r")):
            return self._failure(
                "sandbox_workspace_invalid",
                started=started,
                cleanup_status="not_created",
            )
        image_error = self._require_local_image(request.image)
        if image_error is not None:
            return self._failure(
                image_error,
                started=started,
                cleanup_status="not_created",
            )

        container_id: str | None = None
        execution: CodingSandboxResult | None = None
        try:
            created = self._run(
                self._create_argv(request),
                timeout=min(30.0, float(request.timeout_seconds)),
            )
            if created is None or created.returncode != 0:
                execution = self._failure(
                    "sandbox_create_failed",
                    started=started,
                    cleanup_status="not_created",
                )
            else:
                candidate = created.stdout.strip()
                if _CONTAINER_ID.fullmatch(candidate) is None:
                    execution = self._failure(
                        "sandbox_output_invalid",
                        started=started,
                        cleanup_status="not_created",
                    )
                else:
                    container_id = candidate
                    execution = self._start_and_collect(
                        container_id,
                        request,
                        started=started,
                    )
        finally:
            cleanup_status = self._remove(container_id) if container_id else "not_created"

        if execution is None:
            execution = self._failure(
                "sandbox_start_failed",
                started=started,
                cleanup_status=cleanup_status,
            )
        if cleanup_status == "failed" and execution.status == "passed":
            return execution.model_copy(
                update={
                    "status": "failed",
                    "error_code": "sandbox_cleanup_failed",
                    "cleanup_status": "failed",
                }
            )
        return execution.model_copy(update={"cleanup_status": cleanup_status})

    async def aclose(self) -> None:
        listed = self._run(
            (
                self._docker,
                "ps",
                "-aq",
                "--filter",
                f"label=assistant_agent.coding.owner={self._owner_id}",
            ),
            timeout=10.0,
        )
        if listed is None or listed.returncode != 0:
            return
        for raw_id in listed.stdout.splitlines():
            container_id = raw_id.strip()
            if _CONTAINER_ID.fullmatch(container_id) is not None:
                self._remove(container_id)

    def _require_local_image(self, image: str) -> str | None:
        completed = self._run(
            (
                self._docker,
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            ),
            timeout=15.0,
        )
        if completed is None:
            return "sandbox_unavailable"
        if completed.returncode != 0:
            return "sandbox_image_missing"
        try:
            repo_digests = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return "sandbox_image_mismatch"
        if not isinstance(repo_digests, list) or image not in repo_digests:
            return "sandbox_image_mismatch"
        return None

    def _create_argv(self, request: CodingSandboxRequest) -> tuple[str, ...]:
        tmpfs_bytes = max(1_048_576, min(67_108_864, request.max_disk_bytes // 8))
        mount = f"type=bind,src={request.scratch_root},dst=/workspace,rw"
        label = f"assistant_agent.coding.owner={self._owner_id}"
        argv = (
            self._docker,
            "create",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{self._uid}:{self._gid}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--memory",
            str(request.memory_bytes),
            "--memory-swap",
            str(request.memory_bytes),
            "--cpus",
            str(request.cpu_cores),
            "--pids-limit",
            str(request.max_processes),
            "--ulimit",
            f"cpu={request.cpu_seconds}:{request.cpu_seconds}",
            "--ulimit",
            f"fsize={request.max_file_bytes}:{request.max_file_bytes}",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={tmpfs_bytes},uid={self._uid},gid={self._gid}",
            "--tmpfs",
            f"/home/sandbox:rw,noexec,nosuid,nodev,size={tmpfs_bytes},uid={self._uid},gid={self._gid}",
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "LC_ALL=C.UTF-8",
            "--env",
            "HOME=/home/sandbox",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "GIT_CONFIG_NOSYSTEM=1",
            "--env",
            "GIT_TERMINAL_PROMPT=0",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--label",
            label,
            "--log-driver",
            "none",
            "--init",
            request.image,
            *request.argv,
        )
        return argv

    def _start_and_collect(
        self,
        container_id: str,
        request: CodingSandboxRequest,
        *,
        started: float,
    ) -> CodingSandboxResult:
        timed_out = False
        resource_exceeded = False
        with tempfile.TemporaryDirectory(prefix="assistant-coding-sandbox-") as value:
            temporary = Path(value)
            stdout_path = temporary / "stdout"
            stderr_path = temporary / "stderr"
            try:
                with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                    process = self._runner.popen(
                        (self._docker, "start", "--attach", container_id),
                        stdout=stdout_file,
                        stderr=stderr_file,
                    )
                    deadline = started + request.timeout_seconds
                    while process.poll() is None:
                        if self._monotonic() >= deadline:
                            timed_out = True
                            self._kill(container_id)
                            break
                        if (
                            _file_size(stdout_path) + _file_size(stderr_path)
                            > request.max_output_bytes
                            or _tree_bytes(request.scratch_root) > request.max_disk_bytes
                        ):
                            resource_exceeded = True
                            self._kill(container_id)
                            break
                        time.sleep(self._poll_interval_seconds)
                    process.wait(timeout=10.0)
            except (OSError, subprocess.SubprocessError):
                return self._failure(
                    "sandbox_start_failed",
                    started=started,
                    cleanup_status="removed",
                )

            if (
                _file_size(stdout_path) + _file_size(stderr_path)
                > request.max_output_bytes
                or _tree_bytes(request.scratch_root) > request.max_disk_bytes
            ):
                resource_exceeded = True
            stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
            stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""
            digest = hashlib.sha256(stdout_bytes + b"\x00" + stderr_bytes).hexdigest()
            stdout, stderr, truncated = _bounded_outputs(
                stdout_bytes,
                stderr_bytes,
                request.max_output_bytes,
                request.scratch_root,
                container_id,
            )

        if timed_out:
            return CodingSandboxResult(
                status="timed_out",
                exit_code=None,
                duration_ms=self._duration_ms(started),
                output_digest=digest,
                stdout=stdout,
                stderr=stderr,
                truncated=truncated,
                timed_out=True,
                error_code="sandbox_timeout",
                cleanup_status="removed",
            )
        if resource_exceeded:
            return CodingSandboxResult(
                status="resource_exceeded",
                exit_code=None,
                duration_ms=self._duration_ms(started),
                output_digest=digest,
                stdout=stdout,
                stderr=stderr,
                truncated=True,
                error_code="sandbox_resource_exceeded",
                cleanup_status="removed",
            )

        state = self._inspect_state(container_id)
        if state is None:
            return CodingSandboxResult(
                status="failed",
                exit_code=None,
                duration_ms=self._duration_ms(started),
                output_digest=digest,
                stdout=stdout,
                stderr=stderr,
                truncated=truncated,
                error_code="sandbox_output_invalid",
                cleanup_status="removed",
            )
        exit_code, oom_killed = state
        if oom_killed:
            status = "resource_exceeded"
            error_code = "sandbox_oom_killed"
        elif exit_code != 0:
            status = "failed"
            error_code = "verification_command_failed"
        else:
            status = "passed"
            error_code = None
        return CodingSandboxResult(
            status=status,
            exit_code=exit_code,
            duration_ms=self._duration_ms(started),
            output_digest=digest,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            oom_killed=oom_killed,
            error_code=error_code,
            cleanup_status="removed",
        )

    def _inspect_state(self, container_id: str) -> tuple[int, bool] | None:
        completed = self._run(
            (
                self._docker,
                "inspect",
                "--format",
                "{{json .State}}",
                container_id,
            ),
            timeout=10.0,
        )
        if completed is None or completed.returncode != 0:
            return None
        try:
            state = json.loads(completed.stdout)
            exit_code = state["ExitCode"]
            oom_killed = state["OOMKilled"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if type(exit_code) is not int or type(oom_killed) is not bool:
            return None
        return exit_code, oom_killed

    def _kill(self, container_id: str) -> None:
        self._run((self._docker, "kill", container_id), timeout=10.0)

    def _remove(self, container_id: str | None) -> str:
        if container_id is None or _CONTAINER_ID.fullmatch(container_id) is None:
            return "not_created"
        completed = self._run(
            (self._docker, "rm", "--force", container_id),
            timeout=10.0,
        )
        return "removed" if completed is not None and completed.returncode == 0 else "failed"

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._runner.run(argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    def _duration_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1_000))

    def _failure(
        self,
        error_code: str,
        *,
        started: float,
        cleanup_status: str,
    ) -> CodingSandboxResult:
        return CodingSandboxResult(
            status="failed",
            exit_code=None,
            duration_ms=self._duration_ms(started),
            output_digest=hashlib.sha256(b"").hexdigest(),
            error_code=error_code,
            cleanup_status=cleanup_status,
        )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _tree_bytes(root: Path) -> int:
    total = 0
    try:
        entries = os.scandir(root)
    except OSError:
        return 0
    with entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total += _tree_bytes(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _bounded_outputs(
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    limit: int,
    scratch_root: Path,
    container_id: str,
) -> tuple[str, str, bool]:
    stdout_view = stdout_bytes[:limit]
    remaining = max(0, limit - len(stdout_view))
    stderr_view = stderr_bytes[:remaining]
    truncated = len(stdout_bytes) + len(stderr_bytes) > limit
    replacements = (
        (str(scratch_root).encode(), b"<sandbox-workspace>"),
        (container_id.encode(), b"<sandbox-container>"),
    )
    for marker, replacement in replacements:
        stdout_view = stdout_view.replace(marker, replacement)
        stderr_view = stderr_view.replace(marker, replacement)
    return (
        stdout_view.decode("utf-8", errors="replace"),
        stderr_view.decode("utf-8", errors="replace"),
        truncated,
    )


__all__ = [
    "CodingSandboxBackend",
    "DockerCodingSandboxBackend",
]
