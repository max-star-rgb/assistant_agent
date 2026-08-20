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
    CodingDependencyPlan,
    CodingCredentialRequest,
    CodingArtifactIngressPlan,
)
from assistant_agent.coding.dependencies import (
    build_dependency_plan,
    temporary_wheelhouse,
)
from assistant_agent.coding.dependency_egress import (
    CodingDependencyFetcher,
    CredentialFetchError,
)
from assistant_agent.coding.sandbox import CodingSandboxBackend
from assistant_agent.coding.artifact_egress import ArtifactIngressBackend
from assistant_agent.coding.artifacts import build_artifact_ingress_plan
from assistant_agent.coding.workspace import CodingWorkspaceError, CodingWorkspaceService


class CodingValidationService:
    """Execute only trusted fixed argv in disposable repository copies."""

    def __init__(
        self,
        workspace_service: CodingWorkspaceService,
        sandbox_backend: CodingSandboxBackend | None = None,
        dependency_fetcher: CodingDependencyFetcher | None = None,
        artifact_backend: ArtifactIngressBackend | None = None,
    ) -> None:
        self.workspace_service = workspace_service
        self.sandbox_backend = sandbox_backend
        self.dependency_fetcher = dependency_fetcher
        self.artifact_backend = artifact_backend
        self._root = workspace_service.config.workspace_root / "validation"

    def run(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        *,
        format_round: int,
        dependency_plan: CodingDependencyPlan | None = None,
        credential_request: CodingCredentialRequest | None = None,
        artifact_ingress_plan: CodingArtifactIngressPlan | None = None,
    ) -> CodingVerificationResult:
        if dependency_plan is not None:
            profile = repository.dependency_profile
            if profile is None or self.dependency_fetcher is None:
                return CodingVerificationResult(
                    status="failed",
                    error_code="dependency_egress_unconfigured",
                )
            credential_profile = None
            if profile.credential_profile_id is not None:
                if credential_request is None:
                    return CodingVerificationResult(
                        status="failed",
                        error_code="credential_approval_required",
                    )
                credential_profile = getattr(
                    self.workspace_service.config,
                    "credential_profiles",
                    {},
                ).get(profile.credential_profile_id)
                if credential_profile is None:
                    return CodingVerificationResult(
                        status="failed",
                        error_code="credential_broker_unconfigured",
                    )
            elif credential_request is not None:
                return CodingVerificationResult(
                    status="failed",
                    error_code="credential_approval_mismatch",
                )
            sequence_result: CodingVerificationResult | None = None
            manifest_digest: str | None = None
            try:
                fresh_plan = build_dependency_plan(
                    repository,
                    workspace.root,
                    changed_paths=(dependency_plan.lockfile_path,),
                )
                if (
                    fresh_plan is None
                    or fresh_plan.plan_digest != dependency_plan.plan_digest
                ):
                    raise ValueError("dependency_approval_mismatch")
                with temporary_wheelhouse(self._root) as dependency_root:
                    if credential_request is None:
                        manifest = self.dependency_fetcher.fetch(
                            profile,
                            fresh_plan,
                            workspace.root,
                            dependency_root,
                        )
                    else:
                        manifest = self.dependency_fetcher.fetch(
                            profile,
                            fresh_plan,
                            workspace.root,
                            dependency_root,
                            credential_profile=credential_profile,
                            credential_request=credential_request,
                        )
                    manifest_digest = manifest.manifest_digest
                    sequence_result = self._run_with_artifacts(
                        workspace,
                        repository,
                        format_round=format_round,
                        artifact_ingress_plan=artifact_ingress_plan,
                        dependency_root=dependency_root,
                        dependency_plan=fresh_plan,
                        dependency_manifest_digest=manifest.manifest_digest,
                    )
                    if manifest.credential_profile_id is not None:
                        credential_evidence = {
                            "credential_profile_id": manifest.credential_profile_id,
                            "credential_policy_digest": manifest.credential_policy_digest,
                            "credential_request_digest": manifest.credential_request_digest,
                            "credential_lease_id_digest": manifest.credential_lease_id_digest,
                            "credential_lease_issued_at": manifest.credential_lease_issued_at,
                            "credential_lease_expires_at": manifest.credential_lease_expires_at,
                            "credential_acquire_status": manifest.credential_acquire_status,
                            "credential_inject_status": manifest.credential_inject_status,
                            "credential_cleanup_status": manifest.credential_cleanup_status,
                            "credential_lease_status": manifest.credential_lease_status,
                        }
                        sequence_result = sequence_result.model_copy(
                            update={
                                "evidence": tuple(
                                    item.model_copy(update=credential_evidence)
                                    for item in sequence_result.evidence
                                )
                            }
                        )
                return sequence_result
            except ValueError as exc:
                if sequence_result is not None:
                    return sequence_result.model_copy(
                        update={"status": "failed", "error_code": str(exc)}
                    )
                credential_evidence = (
                    exc.evidence if isinstance(exc, CredentialFetchError) else {}
                )
                evidence = CodingCommandEvidence(
                    command_id="dependency-fetch",
                    kind="build",
                    status="failed",
                    duration_ms=0,
                    output_digest=hashlib.sha256(b"").hexdigest(),
                    stdout="",
                    stderr="",
                    error_code=str(exc),
                    dependency_plan_digest=dependency_plan.plan_digest,
                    dependency_manifest_digest=manifest_digest,
                    dependency_install_status="failed",
                    dependency_install_error=str(exc),
                    **credential_evidence,
                )
                return CodingVerificationResult(
                    status="failed",
                    evidence=(evidence,),
                    error_code=str(exc),
                )
        return self._run_with_artifacts(
            workspace,
            repository,
            format_round=format_round,
            artifact_ingress_plan=artifact_ingress_plan,
        )

    def _run_with_artifacts(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        *,
        format_round: int,
        artifact_ingress_plan: CodingArtifactIngressPlan | None,
        dependency_root: Path | None = None,
        dependency_plan: CodingDependencyPlan | None = None,
        dependency_manifest_digest: str | None = None,
    ) -> CodingVerificationResult:
        if artifact_ingress_plan is None:
            return self._run_sequence(
                workspace,
                repository,
                format_round=format_round,
                dependency_root=dependency_root,
                dependency_plan=dependency_plan,
                dependency_manifest_digest=dependency_manifest_digest,
            )
        profile = repository.artifact_profile
        if profile is None or self.artifact_backend is None:
            return CodingVerificationResult(
                status="failed", error_code="artifact_unconfigured"
            )
        try:
            fresh_plan = build_artifact_ingress_plan(
                repository,
                workspace.root,
                changed_paths=(artifact_ingress_plan.manifest_path,),
            )
            if (
                fresh_plan is None
                or fresh_plan.plan_digest != artifact_ingress_plan.plan_digest
            ):
                raise ValueError("artifact_approval_mismatch")
            self._root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="artifact-ingress-", dir=self._root
            ) as temporary:
                artifact_root = Path(temporary) / "bundle"
                manifest = self.artifact_backend.fetch_scan(
                    profile,
                    fresh_plan,
                    workspace.root,
                    artifact_root,
                )
                return self._run_sequence(
                    workspace,
                    repository,
                    format_round=format_round,
                    dependency_root=dependency_root,
                    dependency_plan=dependency_plan,
                    dependency_manifest_digest=dependency_manifest_digest,
                    artifact_root=artifact_root,
                    artifact_plan=fresh_plan,
                    artifact_manifest=manifest,
                )
        except ValueError as exc:
            evidence = CodingCommandEvidence(
                command_id="artifact-ingress",
                kind="build",
                status="failed",
                duration_ms=0,
                output_digest=hashlib.sha256(b"").hexdigest(),
                stdout="",
                stderr="",
                error_code=str(exc),
                artifact_plan_digest=artifact_ingress_plan.plan_digest,
                artifact_ingress_status="failed",
            )
            return CodingVerificationResult(
                status="failed", evidence=(evidence,), error_code=str(exc)
            )

    def _run_sequence(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        *,
        format_round: int,
        dependency_root: Path | None = None,
        dependency_plan: CodingDependencyPlan | None = None,
        dependency_manifest_digest: str | None = None,
        artifact_root: Path | None = None,
        artifact_plan: CodingArtifactIngressPlan | None = None,
        artifact_manifest=None,
    ) -> CodingVerificationResult:
        evidence: list[CodingCommandEvidence] = []
        for command_id in repository.verification_sequence:
            command = repository.commands[command_id]
            item, patch = self._run_one(
                workspace,
                repository,
                command,
                dependency_root=dependency_root,
                dependency_plan=dependency_plan,
                dependency_manifest_digest=dependency_manifest_digest,
                artifact_root=artifact_root,
                artifact_plan=artifact_plan,
                artifact_manifest=artifact_manifest,
            )
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
        *,
        dependency_root: Path | None = None,
        dependency_plan: CodingDependencyPlan | None = None,
        dependency_manifest_digest: str | None = None,
        artifact_root: Path | None = None,
        artifact_plan: CodingArtifactIngressPlan | None = None,
        artifact_manifest=None,
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
                evidence = self._execute_sandbox(
                    repository,
                    command,
                    scratch,
                    dependency_root=dependency_root,
                    dependency_plan=dependency_plan,
                    dependency_manifest_digest=dependency_manifest_digest,
                    artifact_root=artifact_root,
                    artifact_plan=artifact_plan,
                    artifact_manifest=artifact_manifest,
                )
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
        *,
        dependency_root: Path | None,
        dependency_plan: CodingDependencyPlan | None,
        dependency_manifest_digest: str | None,
        artifact_root: Path | None,
        artifact_plan: CodingArtifactIngressPlan | None,
        artifact_manifest,
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
            max_files=command.max_files,
            max_changed_files=self.workspace_service.config.max_changed_files,
            max_patch_bytes=self.workspace_service.config.max_patch_bytes,
            dependency_root=dependency_root,
            dependency_lockfile_path=(
                dependency_plan.lockfile_path if dependency_plan is not None else None
            ),
            dependency_plan_digest=(
                dependency_plan.plan_digest if dependency_plan is not None else None
            ),
            dependency_manifest_digest=dependency_manifest_digest,
            artifact_root=artifact_root,
            artifact_plan_digest=(
                artifact_plan.plan_digest if artifact_plan is not None else None
            ),
            artifact_manifest_digest=(
                artifact_manifest.manifest_digest
                if artifact_manifest is not None
                else None
            ),
        )
        result = self.sandbox_backend.execute(request)
        if result.status == "passed" and (
            result.formatter_files
            or result.formatter_deletions
            or result.formatter_modes
        ):
            try:
                _materialize_formatter_changes(
                    scratch,
                    result.formatter_files,
                    result.formatter_deletions,
                    result.formatter_modes,
                )
            except OSError:
                return CodingCommandEvidence(
                    command_id=command.command_id,
                    kind=command.kind,
                    status="failed",
                    exit_code=result.exit_code,
                    duration_ms=result.duration_ms,
                    output_digest=result.output_digest,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    truncated=result.truncated,
                    error_code="sandbox_output_invalid",
                    cleanup_status=result.cleanup_status,
                    timed_out=result.timed_out,
                    oom_killed=result.oom_killed,
                )
        return _sandbox_evidence(command, result)


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
        cleanup_status=result.cleanup_status,
        timed_out=result.timed_out,
        oom_killed=result.oom_killed,
        dependency_plan_digest=result.dependency_plan_digest,
        dependency_manifest_digest=result.dependency_manifest_digest,
        dependency_install_status=result.dependency_install_status,
        dependency_install_error=result.dependency_install_error,
        artifact_plan_digest=result.artifact_plan_digest,
        artifact_manifest_digest=result.artifact_manifest_digest,
        artifact_ingress_status=result.artifact_ingress_status,
    )


def _materialize_formatter_changes(
    root: Path,
    files: dict[str, str],
    deletions: tuple[str, ...],
    modes: dict[str, int],
) -> None:
    resolved_root = root.resolve()
    paths = set(files) | set(deletions) | set(modes)
    targets: dict[str, Path] = {}
    for relative in paths:
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise OSError("invalid formatter output path")
        current = root
        for part in parts[:-1]:
            current /= part
            if current.is_symlink():
                raise OSError("formatter output traverses a symlink")
        target = root.joinpath(*parts)
        if not target.resolve(strict=False).is_relative_to(resolved_root):
            raise OSError("formatter output escapes scratch root")
        targets[relative] = target
    for relative in deletions:
        target = targets[relative]
        if target.is_dir():
            raise OSError("formatter output cannot delete a directory")
        target.unlink(missing_ok=True)
    for relative, content in files.items():
        target = targets[relative]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for relative, mode in modes.items():
        targets[relative].chmod(mode)


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
