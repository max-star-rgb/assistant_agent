"""Controlled local Git commit and merge operations for coding mode."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from assistant_agent.coding.config import CodingRepositoryConfig
from assistant_agent.coding.models import (
    CodingCommandEvidence,
    CodingCommitResult,
    CodingWorkspace,
)
from assistant_agent.coding.workspace import CodingWorkspaceError, CodingWorkspaceService


_MAX_GIT_OUTPUT = 65_536


class CodingIntegrationService:
    """Own server-configured Git integration; never expose it as a model Tool."""

    def __init__(self, workspace_service: CodingWorkspaceService) -> None:
        self.workspace_service = workspace_service
        self._locks_guard = threading.Lock()
        self._repo_locks: dict[str, threading.RLock] = {}

    def create_commit(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        *,
        changed_paths: tuple[str, ...],
        verification_evidence: tuple[CodingCommandEvidence, ...],
    ) -> CodingCommitResult:
        if not repository.integration_enabled:
            raise CodingWorkspaceError("integration_not_enabled")
        if not verification_evidence or any(
            item.status != "passed" for item in verification_evidence
        ):
            raise CodingWorkspaceError("verification_required")
        approved_paths = tuple(dict.fromkeys(changed_paths))
        if not approved_paths:
            raise CodingWorkspaceError("commit_empty")
        evidence_digest = _evidence_digest(verification_evidence)
        with self._repo_lock(repository.repo_id):
            head = self.workspace_service.git_head(workspace.root)
            if head != workspace.base_commit:
                return self._existing_commit(
                    workspace,
                    repository,
                    approved_paths,
                    evidence_digest,
                    head,
                )
            actual_paths = _status_paths(workspace.root)
            if actual_paths != set(approved_paths):
                raise CodingWorkspaceError("commit_path_mismatch")
            tree = self._build_tree(workspace, approved_paths)
            message = _commit_message(workspace.workspace_ref, evidence_digest)
            source_commit = _run_git(
                workspace.root,
                "commit-tree",
                tree,
                "-p",
                workspace.base_commit,
                input_text=message,
                env_overrides=_identity_env(repository),
                error_code="commit_create_failed",
            ).strip()
            try:
                _run_git(
                    workspace.root,
                    "read-tree",
                    source_commit,
                    error_code="commit_index_failed",
                )
                _run_git(
                    workspace.root,
                    "update-ref",
                    "HEAD",
                    source_commit,
                    workspace.base_commit,
                    error_code="commit_head_changed",
                )
            except CodingWorkspaceError:
                _run_git(
                    workspace.root,
                    "read-tree",
                    workspace.base_commit,
                    error_code="commit_rollback_failed",
                )
                raise
            if _status_paths(workspace.root):
                raise CodingWorkspaceError("commit_workspace_dirty")
            return CodingCommitResult(
                workspace_ref=workspace.workspace_ref,
                base_commit=workspace.base_commit,
                parent_commit=workspace.base_commit,
                source_commit=source_commit,
                source_tree=tree,
                changed_paths=approved_paths,
                verification_evidence_digest=evidence_digest,
            )

    async def aclose(self) -> None:
        return None

    def _build_tree(
        self,
        workspace: CodingWorkspace,
        approved_paths: tuple[str, ...],
    ) -> str:
        file_descriptor, index_value = tempfile.mkstemp(
            prefix="coding-integration-index-",
            dir=workspace.root.parent,
        )
        os.close(file_descriptor)
        index_path = Path(index_value)
        index_path.unlink()
        env = {"GIT_INDEX_FILE": str(index_path)}
        try:
            _run_git(
                workspace.root,
                "read-tree",
                workspace.base_commit,
                env_overrides=env,
                error_code="commit_index_failed",
            )
            _run_git(
                workspace.root,
                "add",
                "--",
                *approved_paths,
                env_overrides=env,
                error_code="commit_index_failed",
            )
            tree = _run_git(
                workspace.root,
                "write-tree",
                env_overrides=env,
                error_code="commit_tree_failed",
            ).strip()
        finally:
            index_path.unlink(missing_ok=True)
        return tree

    def _existing_commit(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        approved_paths: tuple[str, ...],
        evidence_digest: str,
        head: str,
    ) -> CodingCommitResult:
        if _status_paths(workspace.root):
            raise CodingWorkspaceError("base_commit_changed")
        parent = _run_git(
            workspace.root,
            "rev-parse",
            f"{head}^",
            error_code="base_commit_changed",
        ).strip()
        tree = _run_git(
            workspace.root,
            "rev-parse",
            f"{head}^{{tree}}",
            error_code="base_commit_changed",
        ).strip()
        message = _run_git(
            workspace.root,
            "show",
            "-s",
            "--format=%B",
            head,
            error_code="base_commit_changed",
        ).strip()
        author = _run_git(
            workspace.root,
            "show",
            "-s",
            "--format=%an%x00%ae",
            head,
            error_code="base_commit_changed",
        ).strip().split("\x00")
        changed = tuple(
            item.decode("utf-8")
            for item in _run_git_bytes(
                workspace.root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                workspace.base_commit,
                head,
                error_code="base_commit_changed",
            ).split(b"\x00")
            if item
        )
        if (
            parent != workspace.base_commit
            or changed != approved_paths
            or author != [repository.commit_author_name, repository.commit_author_email]
            or message != _commit_message(workspace.workspace_ref, evidence_digest).strip()
        ):
            raise CodingWorkspaceError("base_commit_changed")
        return CodingCommitResult(
            workspace_ref=workspace.workspace_ref,
            base_commit=workspace.base_commit,
            parent_commit=parent,
            source_commit=head,
            source_tree=tree,
            changed_paths=approved_paths,
            verification_evidence_digest=evidence_digest,
        )

    def _repo_lock(self, repo_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._repo_locks.setdefault(repo_id, threading.RLock())


def _status_paths(repo: Path) -> set[str]:
    output = _run_git_bytes(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        error_code="workspace_git_failed",
    )
    paths: set[str] = set()
    records = [item for item in output.split(b"\x00") if item]
    for record in records:
        if len(record) < 4 or b"R" in record[:2] or b"C" in record[:2]:
            raise CodingWorkspaceError("commit_path_mismatch")
        try:
            paths.add(record[3:].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CodingWorkspaceError("commit_path_mismatch") from exc
    return paths


def _evidence_digest(evidence: tuple[CodingCommandEvidence, ...]) -> str:
    payload = [item.model_dump(mode="json") for item in evidence]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _commit_message(workspace_ref: str, evidence_digest: str) -> str:
    return (
        "assistant-agent: apply approved coding changes\n\n"
        f"Coding-Workspace: {workspace_ref}\n"
        f"Verification-Evidence-Digest: {evidence_digest}\n"
    )


def _identity_env(repository: CodingRepositoryConfig) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": repository.commit_author_name,
        "GIT_AUTHOR_EMAIL": repository.commit_author_email,
        "GIT_COMMITTER_NAME": repository.commit_author_name,
        "GIT_COMMITTER_EMAIL": repository.commit_author_email,
    }


def _run_git(
    repo: Path,
    *args: str,
    error_code: str,
    input_text: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    return _run_git_bytes(
        repo,
        *args,
        error_code=error_code,
        input_bytes=input_text.encode("utf-8") if input_text is not None else None,
        env_overrides=env_overrides,
    ).decode("utf-8")


def _run_git_bytes(
    repo: Path,
    *args: str,
    error_code: str,
    input_bytes: bytes | None = None,
    env_overrides: dict[str, str] | None = None,
) -> bytes:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "commit.gpgSign",
        "GIT_CONFIG_VALUE_1": "false",
        **(env_overrides or {}),
    }
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            capture_output=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodingWorkspaceError(error_code) from exc
    if completed.returncode != 0 or len(completed.stdout) > _MAX_GIT_OUTPUT:
        raise CodingWorkspaceError(error_code)
    return completed.stdout


__all__ = ["CodingIntegrationService"]
