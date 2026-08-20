"""Bounded server-owned validation commands for isolated coding workspaces."""

from __future__ import annotations

import hashlib
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from assistant_agent.coding.config import CodingCommandConfig, CodingRepositoryConfig
from assistant_agent.coding.models import (
    CodingCommandEvidence,
    CodingSandboxRequest,
    CodingSandboxResult,
    CodingVerificationResult,
    CodingWorkspace,
)
from assistant_agent.coding.sandbox import CodingSandboxBackend
from assistant_agent.coding.workspace import CodingWorkspaceError, CodingWorkspaceService


class CodingValidationService:
    """Execute only trusted fixed argv in disposable repository copies."""

    def __init__(
        self,
        workspace_service: CodingWorkspaceService,
        sandbox_backend: CodingSandboxBackend | None = None,
    ) -> None:
        self.workspace_service = workspace_service
        self.sandbox_backend = sandbox_backend
        self._root = workspace_service.config.workspace_root / "validation"

    def run(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        *,
        format_round: int,
    ) -> CodingVerificationResult:
        evidence: list[CodingCommandEvidence] = []
        for command_id in repository.verification_sequence:
            command = repository.commands[command_id]
            item, patch = self._run_one(workspace, repository, command)
            evidence.append(item)
            if item.status != "passed":
                return CodingVerificationResult(
                    status="failed",
                    evidence=tuple(evidence),
                    error_code=item.error_code or "verification_command_failed",
                )
            if command.kind == "format" and patch:
                if format_round >= 1:
                    return CodingVerificationResult(
                        status="failed",
                        evidence=tuple(evidence),
                        error_code="format_not_idempotent",
                    )
                try:
                    validation = self.workspace_service.validate_patch(
                        workspace,
                        patch,
                        f"Apply formatter output from command {command.command_id}",
                    )
                except CodingWorkspaceError as exc:
                    return CodingVerificationResult(
                        status="failed",
                        evidence=tuple(evidence),
                        error_code=exc.code,
                    )
                return CodingVerificationResult(
                    status="format_approval_required",
                    evidence=tuple(evidence),
                    formatter_validation=validation,
                )
        return CodingVerificationResult(status="passed", evidence=tuple(evidence))

    async def aclose(self) -> None:
        return None

    def _run_one(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        command: CodingCommandConfig,
    ) -> tuple[CodingCommandEvidence, str]:
        self._root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"{workspace.workspace_ref[:12]}-",
            dir=self._root,
        ) as temporary_value:
            temporary = Path(temporary_value)
            scratch = temporary / "repo"
            scratch.mkdir()
            _copy_workspace(workspace.root, scratch)
            _git(scratch, "init", "--quiet")
            _git(scratch, "add", "-A", "--")
            if _tree_bytes(temporary) > command.max_disk_bytes:
                return (
                    _empty_evidence(
                        command,
                        status="resource_exceeded",
                        error_code="verification_disk_limit",
                    ),
                    "",
                )
            if repository.sandbox_enabled:
                evidence = self._execute_sandbox(repository, command, scratch)
            else:
                evidence = _execute(command, scratch, temporary)
            if _tree_bytes(temporary) >= command.max_disk_bytes:
                return (
                    evidence.model_copy(
                        update={
                            "status": "resource_exceeded",
                            "error_code": "verification_disk_limit",
                        }
                    ),
                    "",
                )
            if evidence.status != "passed":
                return evidence, ""
            if command.kind != "format":
                return evidence, ""
            return evidence, _formatter_diff(scratch)

    def _execute_sandbox(
        self,
        repository: CodingRepositoryConfig,
        command: CodingCommandConfig,
        scratch: Path,
    ) -> CodingCommandEvidence:
        if self.sandbox_backend is None or repository.sandbox_image is None:
            return _empty_evidence(
                command,
                status="failed",
                error_code="sandbox_unavailable",
            )
        request = CodingSandboxRequest(
            image=repository.sandbox_image,
            argv=command.argv,
            scratch_root=scratch,
            command_id=command.command_id,
            kind=command.kind,
            timeout_seconds=command.timeout_seconds,
            cpu_seconds=command.cpu_seconds,
            cpu_cores=command.cpu_cores,
            memory_bytes=command.memory_bytes,
            max_processes=command.max_processes,
            max_output_bytes=command.max_output_bytes,
            max_file_bytes=self.workspace_service.config.max_file_bytes,
            max_disk_bytes=command.max_disk_bytes,
        )
        return _sandbox_evidence(command, self.sandbox_backend.execute(request))


def _copy_workspace(source: Path, destination: Path) -> None:
    for entry in os.scandir(source):
        if entry.name == ".git" or entry.is_symlink():
            continue
        target = destination / entry.name
        if entry.is_dir(follow_symlinks=False):
            target.mkdir()
            _copy_workspace(Path(entry.path), target)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(entry.path, target)


def _execute(
    command: CodingCommandConfig,
    scratch: Path,
    temporary: Path,
) -> CodingCommandEvidence:
    stdout_path = temporary / "stdout"
    stderr_path = temporary / "stderr"
    home = temporary / "home"
    temp_dir = temporary / "tmp"
    home.mkdir()
    temp_dir.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(temp_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    started = time.monotonic()
    timed_out = False
    return_code: int | None = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                command.argv,
                cwd=scratch,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                start_new_session=True,
                preexec_fn=lambda: _set_limits(command),
            )
            try:
                return_code = process.wait(timeout=command.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                return_code = process.wait()
    except OSError:
        return _empty_evidence(
            command,
            status="failed",
            error_code="verification_process_failed",
            duration_ms=int((time.monotonic() - started) * 1_000),
        )
    duration_ms = int((time.monotonic() - started) * 1_000)
    stdout, stderr, output_digest, truncated = _read_outputs(
        stdout_path,
        stderr_path,
        command.max_output_bytes,
        temporary,
    )
    if timed_out:
        status = "timed_out"
        error_code = "verification_timeout"
    elif return_code == -signal.SIGXFSZ:
        status = "resource_exceeded"
        error_code = "verification_disk_limit"
    elif return_code == -signal.SIGXCPU:
        status = "resource_exceeded"
        error_code = "verification_resource_limit"
    elif return_code != 0:
        status = "failed"
        error_code = "verification_command_failed"
    else:
        status = "passed"
        error_code = None
    return CodingCommandEvidence(
        command_id=command.command_id,
        kind=command.kind,
        status=status,
        exit_code=return_code,
        duration_ms=duration_ms,
        output_digest=output_digest,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
        error_code=error_code,
    )


def _set_limits(command: CodingCommandConfig) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (command.cpu_seconds, command.cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (command.memory_bytes, command.memory_bytes))
    resource.setrlimit(resource.RLIMIT_NPROC, (command.max_processes, command.max_processes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (command.max_disk_bytes, command.max_disk_bytes))
    os.umask(0o077)


def _read_outputs(
    stdout_path: Path,
    stderr_path: Path,
    limit: int,
    temporary: Path,
) -> tuple[str, str, str, bool]:
    stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
    stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""
    digest = hashlib.sha256(stdout_bytes + b"\x00" + stderr_bytes).hexdigest()
    stdout_view = stdout_bytes[:limit]
    remaining = max(0, limit - len(stdout_view))
    stderr_view = stderr_bytes[:remaining]
    truncated = len(stdout_bytes) + len(stderr_bytes) > limit
    marker = str(temporary).encode()
    stdout_view = stdout_view.replace(marker, b"<validation-scratch>")
    stderr_view = stderr_view.replace(marker, b"<validation-scratch>")
    return (
        stdout_view.decode("utf-8", errors="replace"),
        stderr_view.decode("utf-8", errors="replace"),
        digest,
        truncated,
    )


def _empty_evidence(
    command: CodingCommandConfig,
    *,
    status: str,
    error_code: str,
    duration_ms: int = 0,
) -> CodingCommandEvidence:
    return CodingCommandEvidence(
        command_id=command.command_id,
        kind=command.kind,
        status=status,
        exit_code=None,
        duration_ms=duration_ms,
        output_digest=hashlib.sha256(b"").hexdigest(),
        stdout="",
        stderr="",
        error_code=error_code,
    )


def _sandbox_evidence(
    command: CodingCommandConfig,
    result: CodingSandboxResult,
) -> CodingCommandEvidence:
    return CodingCommandEvidence(
        command_id=command.command_id,
        kind=command.kind,
        status=result.status,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        output_digest=result.output_digest,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
        error_code=result.error_code,
    )


def _formatter_diff(scratch: Path) -> str:
    untracked = _git_bytes(
        scratch,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).split(b"\x00")
    paths = [item.decode("utf-8") for item in untracked if item]
    if paths:
        _git(scratch, "add", "-N", "--", *paths)
    return _git_bytes(
        scratch,
        "diff",
        "--no-ext-diff",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--",
    ).decode("utf-8")


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_git_env(),
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise CodingWorkspaceError("verification_git_failed")


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_git_env(),
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise CodingWorkspaceError("verification_git_failed")
    return completed.stdout


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _tree_bytes(root: Path) -> int:
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(current) / name).is_symlink()
        ]
        for name in files:
            candidate = Path(current) / name
            if not candidate.is_symlink():
                total += candidate.stat().st_size
    return total


__all__ = ["CodingValidationService"]
