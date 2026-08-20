"""Fail-closed Docker execution for trusted coding validation commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from typing import BinaryIO, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from assistant_agent.coding.models import CodingSandboxRequest, CodingSandboxResult

_CONTAINER_REFERENCE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_OWNER_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_RUNNER_PATH = "/usr/local/bin/assistant-agent-sandbox-runner"
_PROTOCOL_LABEL = "org.assistant-agent.coding-sandbox-protocol"
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")


class CodingSandboxBackend(Protocol):
    def execute(self, request: CodingSandboxRequest) -> CodingSandboxResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


class DockerCommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def popen(self, argv: tuple[str, ...], *, stdout: int, stderr: int) -> subprocess.Popen[bytes]:
        raise NotImplementedError


class SubprocessDockerCommandRunner:
    def run(self, argv: tuple[str, ...], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=False,
        )

    def popen(self, argv: tuple[str, ...], *, stdout: int, stderr: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
        )


class _RunnerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    protocol_version: Literal[1]
    status: Literal["passed", "failed", "resource_exceeded"]
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: str
    stderr: str
    truncated: bool
    error_code: str | None = None
    formatter_files: dict[str, str] = Field(default_factory=dict)
    formatter_deletions: tuple[str, ...] = ()
    formatter_modes: dict[str, int] = Field(default_factory=dict)

    @field_validator("formatter_deletions", mode="before")
    @classmethod
    def _freeze_deletions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _consistent_result(self) -> "_RunnerPayload":
        if self.status == "passed":
            if self.exit_code != 0 or self.error_code is not None:
                raise ValueError("passed runner payload is inconsistent")
        elif self.error_code is None:
            raise ValueError("failed runner payload requires an error code")
        if self.status != "passed" and (
            self.formatter_files or self.formatter_deletions or self.formatter_modes
        ):
            raise ValueError("failed runner payload cannot contain formatter changes")
        return self


class _PipeBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.exceeded = False
        self.lock = threading.Lock()

    def retain(self, chunk: bytes) -> bytes:
        with self.lock:
            remaining = max(0, self.limit - self.total)
            self.total += len(chunk)
            self.exceeded = self.total > self.limit
            return chunk[:remaining]


class _PipeCollector(threading.Thread):
    def __init__(self, stream: BinaryIO, budget: _PipeBudget) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.budget = budget
        self.buffer = bytearray()

    def run(self) -> None:
        while True:
            chunk = self.stream.read(65_536)
            if not chunk:
                return
            retained = self.budget.retain(chunk)
            if retained:
                self.buffer.extend(retained)


class DockerCodingSandboxBackend:
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
        name_factory: Callable[[], str] | None = None,
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
        self._name_factory = name_factory or (
            lambda: f"assistant-coding-{resolved_owner[:20]}-{uuid.uuid4().hex[:16]}"
        )

    def execute(self, request: CodingSandboxRequest) -> CodingSandboxResult:
        started = self._monotonic()
        if self._uid <= 0 or self._gid < 0:
            return self._failure("sandbox_user_invalid", started, "not_created")
        scratch_value = str(request.scratch_root)
        if (
            not request.scratch_root.is_absolute()
            or not request.scratch_root.is_dir()
            or any(item in scratch_value for item in (",", "\x00", "\n", "\r"))
        ):
            return self._failure("sandbox_workspace_invalid", started, "not_created")
        image_error = self._require_local_image(request.image)
        if image_error is not None:
            return self._failure(image_error, started, "not_created")
        container_name = self._name_factory()
        if _CONTAINER_REFERENCE.fullmatch(container_name) is None:
            return self._failure("sandbox_create_failed", started, "not_created")

        create_succeeded = False
        try:
            created = self._run(
                self._create_argv(request, container_name),
                timeout=min(30.0, float(request.timeout_seconds)),
            )
            if created is None or created.returncode != 0:
                execution = self._failure("sandbox_create_failed", started, "not_created")
            else:
                create_succeeded = True
                copied = self._run(
                    (
                        self._docker,
                        "cp",
                        "--archive",
                        f"{request.scratch_root}/.",
                        f"{container_name}:/input",
                    ),
                    timeout=min(30.0, float(request.timeout_seconds)),
                )
                container_id = self._inspect_id(container_name)
                if copied is None or copied.returncode != 0:
                    execution = self._failure(
                        "sandbox_input_copy_failed",
                        started,
                        "removed",
                    )
                elif container_id is None:
                    execution = self._failure(
                        "sandbox_create_failed",
                        started,
                        "removed",
                    )
                else:
                    execution = self._start_and_collect(
                        container_name,
                        container_id,
                        request,
                        started,
                    )
        finally:
            cleanup_status = self._remove(
                container_name,
                allow_absent=not create_succeeded,
            )
        if cleanup_status == "failed" and execution.status == "passed":
            return execution.model_copy(update={
                "status": "failed", "error_code": "sandbox_cleanup_failed",
                "cleanup_status": "failed",
            })
        return execution.model_copy(update={"cleanup_status": cleanup_status})

    async def aclose(self) -> None:
        listed = self._run((
            self._docker, "ps", "-aq", "--filter",
            f"label=assistant_agent.coding.owner={self._owner_id}",
        ), timeout=10.0)
        if listed is None or listed.returncode != 0:
            return
        for raw_reference in listed.stdout.splitlines():
            reference = raw_reference.strip()
            if _CONTAINER_REFERENCE.fullmatch(reference) is not None:
                self._remove(reference)

    def _require_local_image(self, image: str) -> str | None:
        completed = self._run((
            self._docker, "image", "inspect", "--format",
            "{{json .RepoDigests}}\n{{json .Config.Labels}}", image,
        ), timeout=15.0)
        if completed is None:
            return "sandbox_unavailable"
        if completed.returncode != 0:
            return "sandbox_image_missing"
        lines = completed.stdout.splitlines()
        if len(lines) != 2:
            return "sandbox_image_mismatch"
        try:
            repo_digests, labels = json.loads(lines[0]), json.loads(lines[1])
        except json.JSONDecodeError:
            return "sandbox_image_mismatch"
        if (
            not isinstance(repo_digests, list) or image not in repo_digests
            or not isinstance(labels, dict) or labels.get(_PROTOCOL_LABEL) != "1"
        ):
            return "sandbox_image_mismatch"
        return None

    def _create_argv(self, request: CodingSandboxRequest, container_name: str) -> tuple[str, ...]:
        auxiliary = max(1_048_576, min(67_108_864, request.max_disk_bytes // 8))
        inode_limit = request.max_files + request.max_changed_files + 64
        workspace_tmpfs = (
            "/workspace:rw,nosuid,nodev,"
            f"size={request.max_disk_bytes},nr_inodes={inode_limit},"
            f"uid={self._uid},gid={self._gid}"
        )
        return (
            self._docker, "create", "--name", container_name, "--hostname", "sandbox",
            "--network", "none", "--cgroupns", "private", "--read-only",
            "--user", f"{self._uid}:{self._gid}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--memory", str(request.memory_bytes), "--memory-swap", str(request.memory_bytes),
            "--cpus", str(request.cpu_cores), "--pids-limit", str(request.max_processes),
            "--ulimit", f"cpu={request.cpu_seconds}:{request.cpu_seconds}",
            "--ulimit", f"fsize={request.max_file_bytes}:{request.max_file_bytes}",
            "--tmpfs", workspace_tmpfs,
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={auxiliary},uid={self._uid},gid={self._gid}",
            "--tmpfs", f"/home/sandbox:rw,noexec,nosuid,nodev,size={auxiliary},uid={self._uid},gid={self._gid}",
            "--workdir", "/workspace",
            "--env", "LANG=C.UTF-8", "--env", "LC_ALL=C.UTF-8",
            "--env", "HOME=/home/sandbox", "--env", "TMPDIR=/tmp",
            "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_TERMINAL_PROMPT=0",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--label", f"assistant_agent.coding.owner={self._owner_id}",
            "--log-driver", "none", "--init", request.image, _RUNNER_PATH,
            "--kind", request.kind, "--max-output-bytes", str(request.max_output_bytes),
            "--max-file-bytes", str(request.max_file_bytes),
            "--max-disk-bytes", str(request.max_disk_bytes),
            "--max-files", str(request.max_files),
            "--max-changed-files", str(request.max_changed_files),
            "--max-patch-bytes", str(request.max_patch_bytes), "--", *request.argv,
        )

    def _start_and_collect(
        self,
        container_name: str,
        container_id: str,
        request: CodingSandboxRequest,
        started: float,
    ) -> CodingSandboxResult:
        try:
            process = self._runner.popen(
                (self._docker, "start", "--attach", container_name),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.SubprocessError):
            return self._failure("sandbox_start_failed", started, "removed")
        stdout_stream, stderr_stream = getattr(process, "stdout", None), getattr(process, "stderr", None)
        if stdout_stream is None or stderr_stream is None:
            self._kill(container_name)
            return self._failure("sandbox_start_failed", started, "removed")
        budget = _PipeBudget(
            request.max_output_bytes + request.max_patch_bytes
            + request.max_changed_files * 1_024 + 65_536
        )
        stdout_collector = _PipeCollector(stdout_stream, budget)
        stderr_collector = _PipeCollector(stderr_stream, budget)
        stdout_collector.start()
        stderr_collector.start()
        timed_out = overflow = False
        deadline = started + request.timeout_seconds
        while process.poll() is None:
            if self._monotonic() >= deadline:
                timed_out = True
                self._kill(container_name)
                break
            if budget.exceeded:
                overflow = True
                self._kill(container_name)
                break
            time.sleep(self._poll_interval_seconds)
        try:
            attach_returncode = process.wait(timeout=10.0)
        except subprocess.SubprocessError:
            self._kill(container_name)
            return self._failure("sandbox_start_failed", started, "removed")
        stdout_collector.join(timeout=5)
        stderr_collector.join(timeout=5)
        if stdout_collector.is_alive() or stderr_collector.is_alive():
            return self._failure("sandbox_start_failed", started, "removed")
        if timed_out:
            return self._failure("sandbox_timeout", started, "removed", status="timed_out", timed_out=True)
        if overflow or budget.exceeded:
            return self._failure("sandbox_resource_exceeded", started, "removed", status="resource_exceeded")
        state = self._inspect_state(container_name)
        if state is None:
            return self._failure("sandbox_output_invalid", started, "removed")
        exit_code, oom_killed, running = state
        if running:
            return self._failure("sandbox_start_failed", started, "removed")
        if oom_killed:
            return self._failure(
                "sandbox_oom_killed", started, "removed",
                status="resource_exceeded", oom_killed=True,
            )
        if attach_returncode != 0 or exit_code != 0:
            return self._failure("sandbox_start_failed", started, "removed")
        if bytes(stderr_collector.buffer):
            return self._failure("sandbox_output_invalid", started, "removed")
        try:
            payload = _RunnerPayload.model_validate(json.loads(bytes(stdout_collector.buffer)))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            return self._failure("sandbox_output_invalid", started, "removed")
        formatter_changes = self._validate_formatter_changes(payload, request)
        if formatter_changes is None:
            return self._failure("sandbox_output_invalid", started, "removed")
        formatter_files, formatter_deletions, formatter_modes = formatter_changes
        return CodingSandboxResult(
            status=payload.status, exit_code=payload.exit_code,
            duration_ms=payload.duration_ms, output_digest=payload.output_digest,
            stdout=_redact(
                payload.stdout,
                container_name,
                container_id,
                str(request.scratch_root),
            ),
            stderr=_redact(
                payload.stderr,
                container_name,
                container_id,
                str(request.scratch_root),
            ),
            truncated=payload.truncated,
            error_code=payload.error_code, cleanup_status="removed",
            formatter_files=formatter_files,
            formatter_deletions=formatter_deletions,
            formatter_modes=formatter_modes,
        )

    def _validate_formatter_changes(
        self, payload: _RunnerPayload, request: CodingSandboxRequest,
    ) -> tuple[dict[str, str], tuple[str, ...], dict[str, int]] | None:
        paths = (
            set(payload.formatter_files)
            | set(payload.formatter_deletions)
            | set(payload.formatter_modes)
        )
        if request.kind != "format" and paths:
            return None
        if len(paths) > request.max_changed_files:
            return None
        total = 0
        validated: dict[str, str] = {}
        for relative in sorted(paths):
            parts = relative.split("/")
            if (
                not relative or relative.startswith("/") or ".." in parts or ".git" in parts
                or any(item in relative for item in ("\x00", "\n", "\r"))
            ):
                return None
            total += len(relative.encode())
        for relative, content in payload.formatter_files.items():
            content_bytes = content.encode("utf-8")
            total += len(content_bytes)
            if len(content_bytes) > request.max_file_bytes or total > request.max_patch_bytes:
                return None
            validated[relative] = content
        if total > request.max_patch_bytes:
            return None
        if len(payload.formatter_deletions) != len(set(payload.formatter_deletions)):
            return None
        for mode in payload.formatter_modes.values():
            if type(mode) is not int or mode < 0 or mode > 0o777:
                return None
        return validated, payload.formatter_deletions, dict(payload.formatter_modes)

    def _inspect_state(self, reference: str) -> tuple[int, bool, bool] | None:
        completed = self._run((
            self._docker, "inspect", "--format", "{{json .State}}", reference,
        ), timeout=10.0)
        if completed is None or completed.returncode != 0:
            return None
        try:
            state = json.loads(completed.stdout)
            values = state["ExitCode"], state["OOMKilled"], state["Running"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if type(values[0]) is not int or type(values[1]) is not bool or type(values[2]) is not bool:
            return None
        return values

    def _inspect_id(self, reference: str) -> str | None:
        completed = self._run(
            (self._docker, "inspect", "--format", "{{.Id}}", reference),
            timeout=10.0,
        )
        if completed is None or completed.returncode != 0:
            return None
        value = completed.stdout.strip()
        return value if _CONTAINER_ID.fullmatch(value) is not None else None

    def _kill(self, reference: str) -> None:
        self._run((self._docker, "kill", reference), timeout=10.0)

    def _remove(self, reference: str, *, allow_absent: bool = False) -> str:
        if _CONTAINER_REFERENCE.fullmatch(reference) is None:
            return "failed"
        removed = self._run((self._docker, "rm", "--force", reference), timeout=10.0)
        if removed is not None and removed.returncode == 0:
            return "removed"
        if (
            allow_absent
            and removed is not None
            and removed.returncode != 0
            and "no such container" in removed.stderr.lower()
        ):
            return "not_created"
        return "failed"

    def _run(
        self, argv: tuple[str, ...], *, timeout: float,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._runner.run(argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    def _failure(
        self, error_code: str, started: float, cleanup_status: str, *,
        status: str = "failed", timed_out: bool = False, oom_killed: bool = False,
    ) -> CodingSandboxResult:
        return CodingSandboxResult(
            status=status, exit_code=None,
            duration_ms=max(0, int((self._monotonic() - started) * 1_000)),
            output_digest=hashlib.sha256(b"").hexdigest(), timed_out=timed_out,
            oom_killed=oom_killed, error_code=error_code, cleanup_status=cleanup_status,
        )


def _redact(
    value: str,
    container_name: str,
    container_id: str,
    scratch_root: str,
) -> str:
    return value.replace(scratch_root, "<sandbox-workspace>").replace(
        container_id,
        "<sandbox-container>",
    ).replace(
        container_name,
        "<sandbox-container>",
    )


__all__ = ["CodingSandboxBackend", "DockerCodingSandboxBackend"]
