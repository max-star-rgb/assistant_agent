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
    CodingMergePreview,
    CodingMergeResult,
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

    def prepare_merge(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        commit: CodingCommitResult,
    ) -> CodingMergePreview:
        if not repository.integration_enabled:
            raise CodingWorkspaceError("integration_not_enabled")
        with self._repo_lock(repository.repo_id):
            self._validate_source_commit(workspace, commit)
            target_head = self._target_preflight(repository)
            ancestor = _git_exit_code(
                repository.path,
                "merge-base",
                "--is-ancestor",
                target_head,
                commit.source_commit,
                error_code="merge_preflight_failed",
            )
            if ancestor == 0:
                strategy = "fast_forward"
                result_tree = commit.source_tree
                result_commit = commit.source_commit
            elif ancestor == 1:
                completed = _git_completed(
                    repository.path,
                    "merge-tree",
                    "--write-tree",
                    target_head,
                    commit.source_commit,
                )
                if completed.returncode != 0:
                    raise CodingWorkspaceError("merge_conflict")
                try:
                    result_tree = completed.stdout.decode("utf-8").splitlines()[0].strip()
                except (UnicodeDecodeError, IndexError) as exc:
                    raise CodingWorkspaceError("merge_preflight_failed") from exc
                if not _is_object_id(result_tree):
                    raise CodingWorkspaceError("merge_preflight_failed")
                result_commit = _run_git(
                    repository.path,
                    "commit-tree",
                    result_tree,
                    "-p",
                    target_head,
                    "-p",
                    commit.source_commit,
                    input_text=_merge_message(workspace.workspace_ref),
                    env_overrides=_identity_env(repository),
                    error_code="merge_preview_failed",
                ).strip()
                strategy = "merge_commit"
            else:
                raise CodingWorkspaceError("merge_preflight_failed")
            facts = {
                "source_commit": commit.source_commit,
                "expected_target_head": target_head,
                "target_branch": repository.target_branch,
                "strategy": strategy,
                "result_tree": result_tree,
                "result_commit": result_commit,
            }
            return CodingMergePreview(
                **facts,
                merge_preview_digest=_canonical_digest(facts),
            )

    def apply_merge(
        self,
        workspace: CodingWorkspace,
        repository: CodingRepositoryConfig,
        preview: CodingMergePreview,
    ) -> CodingMergeResult:
        if not repository.integration_enabled:
            raise CodingWorkspaceError("integration_not_enabled")
        facts = preview.model_dump(exclude={"merge_preview_digest"})
        if _canonical_digest(facts) != preview.merge_preview_digest:
            raise CodingWorkspaceError("merge_preview_mismatch")
        with self._repo_lock(repository.repo_id):
            self._validate_preview_objects(repository, preview)
            current_head = self._target_preflight(repository)
            if current_head == preview.result_commit:
                return _merge_result(preview)
            if current_head != preview.expected_target_head:
                raise CodingWorkspaceError("target_head_changed")
            try:
                _run_git(
                    repository.path,
                    "merge",
                    "--ff-only",
                    "--no-edit",
                    preview.result_commit,
                    error_code="merge_apply_failed",
                )
            except CodingWorkspaceError:
                if (
                    self.workspace_service.git_head(repository.path) != current_head
                    or _status_paths(repository.path)
                ):
                    raise CodingWorkspaceError("merge_rollback_failed")
                raise
            if (
                self.workspace_service.git_head(repository.path) != preview.result_commit
                or _status_paths(repository.path)
            ):
                raise CodingWorkspaceError("merge_apply_failed")
            return _merge_result(preview)

    async def aclose(self) -> None:
        return None

    def _validate_source_commit(
        self,
        workspace: CodingWorkspace,
        commit: CodingCommitResult,
    ) -> None:
        if (
            commit.workspace_ref != workspace.workspace_ref
            or commit.base_commit != workspace.base_commit
            or commit.parent_commit != workspace.base_commit
            or self.workspace_service.git_head(workspace.root) != commit.source_commit
            or _status_paths(workspace.root)
        ):
            raise CodingWorkspaceError("source_commit_changed")
        tree = _run_git(
            workspace.root,
            "rev-parse",
            f"{commit.source_commit}^{{tree}}",
            error_code="source_commit_changed",
        ).strip()
        if tree != commit.source_tree:
            raise CodingWorkspaceError("source_commit_changed")

    def _target_preflight(self, repository: CodingRepositoryConfig) -> str:
        root = _run_git(
            repository.path,
            "rev-parse",
            "--show-toplevel",
            error_code="target_repository_invalid",
        ).strip()
        if Path(root).resolve() != repository.path.resolve():
            raise CodingWorkspaceError("target_repository_invalid")
        branch = _run_git(
            repository.path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            error_code="target_branch_mismatch",
        ).strip()
        if branch != repository.target_branch:
            raise CodingWorkspaceError("target_branch_mismatch")
        if _status_paths(repository.path):
            raise CodingWorkspaceError("target_worktree_dirty")
        return _run_git(
            repository.path,
            "rev-parse",
            "--verify",
            f"refs/heads/{repository.target_branch}^{{commit}}",
            error_code="target_branch_mismatch",
        ).strip()

    def _validate_preview_objects(
        self,
        repository: CodingRepositoryConfig,
        preview: CodingMergePreview,
    ) -> None:
        if preview.target_branch != repository.target_branch:
            raise CodingWorkspaceError("merge_preview_mismatch")
        result_tree = _run_git(
            repository.path,
            "rev-parse",
            f"{preview.result_commit}^{{tree}}",
            error_code="merge_preview_mismatch",
        ).strip()
        source_tree = _run_git(
            repository.path,
            "rev-parse",
            f"{preview.source_commit}^{{tree}}",
            error_code="merge_preview_mismatch",
        ).strip()
        if result_tree != preview.result_tree:
            raise CodingWorkspaceError("merge_preview_mismatch")
        if preview.strategy == "fast_forward":
            if (
                preview.result_commit != preview.source_commit
                or result_tree != source_tree
                or _git_exit_code(
                    repository.path,
                    "merge-base",
                    "--is-ancestor",
                    preview.expected_target_head,
                    preview.result_commit,
                    error_code="merge_preview_mismatch",
                )
                != 0
            ):
                raise CodingWorkspaceError("merge_preview_mismatch")
            return
        parents = _run_git(
            repository.path,
            "show",
            "-s",
            "--format=%P",
            preview.result_commit,
            error_code="merge_preview_mismatch",
        ).strip().split()
        if parents != [preview.expected_target_head, preview.source_commit]:
            raise CodingWorkspaceError("merge_preview_mismatch")

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


def _merge_message(workspace_ref: str) -> str:
    return (
        "assistant-agent: merge approved coding changes\n\n"
        f"Coding-Workspace: {workspace_ref}\n"
    )


def _canonical_digest(facts: dict[str, object]) -> str:
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _merge_result(preview: CodingMergePreview) -> CodingMergeResult:
    return CodingMergeResult(
        source_commit=preview.source_commit,
        previous_target_head=preview.expected_target_head,
        result_commit=preview.result_commit,
        result_tree=preview.result_tree,
        target_branch=preview.target_branch,
        strategy=preview.strategy,
        merge_preview_digest=preview.merge_preview_digest,
    )


def _is_object_id(value: str) -> bool:
    return 40 <= len(value) <= 64 and all(character in "0123456789abcdef" for character in value)


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
    completed = _git_completed(
        repo,
        *args,
        input_bytes=input_bytes,
        env_overrides=env_overrides,
    )
    if completed.returncode != 0 or len(completed.stdout) > _MAX_GIT_OUTPUT:
        raise CodingWorkspaceError(error_code)
    return completed.stdout


def _git_exit_code(
    repo: Path,
    *args: str,
    error_code: str,
) -> int:
    completed = _git_completed(repo, *args)
    if completed.returncode < 0 or completed.returncode > 1:
        raise CodingWorkspaceError(error_code)
    return completed.returncode


def _git_completed(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
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
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            capture_output=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodingWorkspaceError("workspace_git_failed") from exc


__all__ = ["CodingIntegrationService"]
