"""Thread-scoped Git worktrees and governed read operations."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import selectors
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.models import (
    CodingAnalysisSnapshot,
    CodingDiffResult,
    CodingListEntry,
    CodingListResult,
    CodingPatchApplyResult,
    CodingPatchProposal,
    CodingPatchValidation,
    CodingReadResult,
    CodingRepairApprovalContext,
    CodingSearchMatch,
    CodingSearchResult,
    CodingStatusResult,
    CodingWorkspace,
    CodingWorkspaceMetadata,
)
from assistant_agent.coding.patches import CodingPatchError, parse_coding_patch
from assistant_agent.coding.policy import CodingPathPolicy, CodingPolicyError


_SECRET_FILE = ".workspace-key"
_METADATA_FILE = "metadata.json"
_LOCK_FILE = "workspace.lock"
_REPO_DIR = "repo"
_ANALYSIS_SNAPSHOTS_DIR = "analysis-snapshots"
_ANALYSIS_SNAPSHOT_METADATA_FILE = "metadata.json"
_ANALYSIS_SNAPSHOT_TREE_DIR = "tree"
_ANALYSIS_BUILD_PREFIX = ".analysis-build-"
_ANALYSIS_QUARANTINE_PREFIX = ".analysis-quarantine-"
_ANALYSIS_QUARANTINE_SECONDS = 300
_ANALYSIS_METADATA_TEMP_PREFIX = ".metadata-"
_MAX_GIT_OUTPUT = 262_144


class CodingWorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message or 'coding workspace operation failed'}")


class _AnalysisSnapshotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["coding_analysis_snapshot_v1"] = (
        "coding_analysis_snapshot_v1"
    )
    snapshot: CodingAnalysisSnapshot
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    thread_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo_id: str = Field(min_length=1, max_length=80)
    tree_object: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    status: CodingStatusResult
    diff: CodingDiffResult
    active_lease: bool = True


class CodingWorkspaceService:
    def __init__(
        self,
        config: CodingConfig,
        *,
        policy: CodingPathPolicy | None = None,
        secret: bytes | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.policy = policy or CodingPathPolicy()
        self._provided_secret = secret
        self._cached_secret: bytes | None = secret
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(self, identity: str, thread_id: str, repo_id: str) -> CodingWorkspace:
        if not self.config.enabled or repo_id not in self.config.repositories:
            raise CodingWorkspaceError("workspace_not_allowed")
        if not identity.strip() or not thread_id.strip():
            raise CodingWorkspaceError("workspace_identity_mismatch")
        self.cleanup_expired()
        self.config.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_ref = self._workspace_ref(identity, thread_id, repo_id)
        management_root = self._management_root(workspace_ref)
        management_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        with self._lock(workspace_ref):
            metadata_path = management_root / _METADATA_FILE
            if metadata_path.exists():
                metadata = self._load_metadata(metadata_path)
                self._verify_scope(metadata, identity=identity, thread_id=thread_id)
                if metadata.frozen:
                    raise CodingWorkspaceError("workspace_frozen")
                metadata = metadata.model_copy(
                    update={"expires_at": self._clock() + timedelta(seconds=self.config.ttl_seconds)}
                )
                self._write_metadata(metadata_path, metadata)
                return self._workspace(metadata)

            source_repo = self.config.repositories[repo_id].path
            base_commit = self.git_head(source_repo)
            repo_root = management_root / _REPO_DIR
            self._run_git(
                source_repo,
                "worktree",
                "add",
                "--detach",
                str(repo_root),
                base_commit,
                error_code="workspace_create_failed",
            )
            now = self._clock()
            metadata = CodingWorkspaceMetadata(
                workspace_ref=workspace_ref,
                identity_digest=_digest(identity),
                thread_digest=_digest(thread_id),
                repo_id=repo_id,
                base_commit=base_commit,
                created_at=now,
                expires_at=now + timedelta(seconds=self.config.ttl_seconds),
            )
            self._write_metadata(metadata_path, metadata)
            return self._workspace(metadata)

    def get(
        self,
        workspace_ref: str,
        *,
        identity: str,
        thread_id: str,
    ) -> CodingWorkspace:
        metadata_path = self._management_root(workspace_ref) / _METADATA_FILE
        if not metadata_path.is_file():
            raise CodingWorkspaceError("workspace_not_allowed")
        metadata = self._load_metadata(metadata_path)
        self._verify_scope(metadata, identity=identity, thread_id=thread_id)
        if metadata.frozen:
            raise CodingWorkspaceError("workspace_frozen")
        if metadata.expires_at <= self._clock():
            raise CodingWorkspaceError("workspace_expired")
        workspace = self._workspace(metadata)
        if not workspace.root.is_dir():
            raise CodingWorkspaceError("workspace_not_allowed")
        return workspace

    def git_head(self, repo: Path) -> str:
        return self._run_git(repo, "rev-parse", "HEAD", error_code="workspace_git_failed").strip()

    def create_analysis_snapshot(
        self,
        workspace: CodingWorkspace,
        *,
        identity: str,
        thread_id: str,
    ) -> CodingAnalysisSnapshot:
        self._require_analysis_workspace_scope(
            workspace,
            identity=identity,
            thread_id=thread_id,
        )
        management_root = self._management_root(workspace.workspace_ref)
        snapshots_root = management_root / _ANALYSIS_SNAPSHOTS_DIR
        index_path: Path | None = None
        temporary_root: Path | None = None
        object_root: Path | None = None
        published_root: Path | None = None
        published_created = False
        with self._lock(workspace.workspace_ref):
            try:
                self._require_base_commit(workspace)
                snapshots_root.mkdir(mode=0o700, exist_ok=True)
                temporary_root = Path(
                    tempfile.mkdtemp(
                        prefix=".analysis-build-",
                        dir=snapshots_root,
                    )
                )
                index_path = temporary_root / "index"
                object_root = temporary_root / "objects"
                object_root.mkdir(mode=0o700)
                git_env = self._analysis_git_environment(
                    workspace,
                    index_path=index_path,
                    object_root=object_root,
                )
                self._run_git(
                    workspace.root,
                    "read-tree",
                    "HEAD",
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                )
                baseline_files = self._normalize_analysis_index(workspace, git_env)
                baseline_tree_object = self._run_git(
                    workspace.root,
                    "write-tree",
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                ).strip()
                self._run_git(
                    workspace.root,
                    "read-tree",
                    "--empty",
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                )
                current_files = self._build_analysis_worktree_index(
                    workspace,
                    git_env,
                )
                tree_object = self._run_git(
                    workspace.root,
                    "write-tree",
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                ).strip()
                diff_result, workspace_diff_digest = self._analysis_git_diff_result(
                    workspace,
                    baseline_tree_object,
                    tree_object,
                    git_env,
                )
                status = self._analysis_status_result(baseline_files, current_files)
                tree_digest = _digest(tree_object)
                identity_digest = _digest(identity)
                thread_digest = _digest(thread_id)
                snapshot_ref = self._analysis_snapshot_ref(
                    identity_digest=identity_digest,
                    thread_digest=thread_digest,
                    workspace_ref=workspace.workspace_ref,
                    tree_object=tree_object,
                    workspace_diff_digest=workspace_diff_digest,
                )
                now = self._clock()
                expires_at = min(
                    workspace.expires_at,
                    now + timedelta(seconds=self.config.ttl_seconds),
                )
                if expires_at <= now:
                    raise CodingWorkspaceError("coding_analysis_snapshot_expired")
                snapshot = CodingAnalysisSnapshot(
                    snapshot_ref=snapshot_ref,
                    workspace_ref=workspace.workspace_ref,
                    base_commit=workspace.base_commit,
                    tree_digest=tree_digest,
                    workspace_diff_digest=workspace_diff_digest,
                    created_at=now,
                    expires_at=expires_at,
                )
                metadata = _AnalysisSnapshotMetadata(
                    snapshot=snapshot,
                    identity_digest=identity_digest,
                    thread_digest=thread_digest,
                    workspace_digest=_digest(workspace.workspace_ref),
                    repo_id=workspace.repo_id,
                    tree_object=tree_object,
                    status=status,
                    diff=diff_result,
                )
                published_root = self._analysis_snapshot_root(snapshot)
                if published_root.exists():
                    existing = self._load_analysis_snapshot_metadata(
                        published_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
                    )
                    try:
                        self._verify_analysis_snapshot_metadata(
                            existing.snapshot,
                            existing,
                            published_root,
                            require_active=False,
                        )
                    except CodingWorkspaceError as exc:
                        if exc.code != "coding_analysis_snapshot_expired":
                            raise
                        self._remove_analysis_snapshot_directory(published_root)
                    else:
                        if (
                            not hmac.compare_digest(
                                existing.identity_digest,
                                identity_digest,
                            )
                            or not hmac.compare_digest(
                                existing.thread_digest,
                                thread_digest,
                            )
                            or not hmac.compare_digest(
                                existing.workspace_digest,
                                _digest(workspace.workspace_ref),
                            )
                            or existing.repo_id != workspace.repo_id
                            or existing.tree_object != tree_object
                            or existing.snapshot.snapshot_ref != snapshot.snapshot_ref
                            or existing.snapshot.workspace_ref != snapshot.workspace_ref
                            or existing.snapshot.base_commit != snapshot.base_commit
                            or existing.snapshot.tree_digest != snapshot.tree_digest
                            or existing.snapshot.workspace_diff_digest
                            != snapshot.workspace_diff_digest
                        ):
                            raise CodingWorkspaceError(
                                "coding_analysis_snapshot_mismatch"
                            )
                        if self._cleanup_analysis_build_resources(
                            index_path=index_path,
                            object_root=object_root,
                            temporary_root=temporary_root,
                        ):
                            raise CodingWorkspaceError(
                                "coding_analysis_snapshot_failed"
                            )
                        index_path = None
                        object_root = None
                        temporary_root = None
                        if not existing.active_lease:
                            existing = existing.model_copy(
                                update={"active_lease": True}
                            )
                            self._write_analysis_snapshot_metadata(
                                published_root / _ANALYSIS_SNAPSHOT_METADATA_FILE,
                                existing,
                            )
                        return existing.snapshot

                tree_root = temporary_root / _ANALYSIS_SNAPSHOT_TREE_DIR
                tree_root.mkdir(mode=0o700)
                self._materialize_analysis_files(tree_root, current_files)
                self._write_analysis_snapshot_metadata(
                    temporary_root / _ANALYSIS_SNAPSHOT_METADATA_FILE,
                    metadata,
                )
                self._make_analysis_tree_read_only(tree_root)
                if self._cleanup_analysis_build_resources(
                    index_path=index_path,
                    object_root=object_root,
                    temporary_root=None,
                ):
                    raise CodingWorkspaceError("coding_analysis_snapshot_failed")
                index_path = None
                object_root = None
                os.replace(temporary_root, published_root)
                temporary_root = None
                published_created = True
                return snapshot
            except CodingWorkspaceError as exc:
                cleanup_failed = self._cleanup_analysis_build_resources(
                    index_path=index_path,
                    object_root=object_root,
                    temporary_root=temporary_root,
                )
                if published_created and published_root is not None:
                    self._deactivate_analysis_snapshot_best_effort(published_root)
                if cleanup_failed:
                    raise CodingWorkspaceError(
                        "coding_analysis_snapshot_failed"
                    ) from exc
                raise
            except Exception as exc:
                self._cleanup_analysis_build_resources(
                    index_path=index_path,
                    object_root=object_root,
                    temporary_root=temporary_root,
                )
                if published_created and published_root is not None:
                    self._deactivate_analysis_snapshot_best_effort(published_root)
                raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc

    def resolve_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> CodingWorkspace:
        snapshot_root = self._analysis_snapshot_root(snapshot)
        with self._lock(snapshot.workspace_ref):
            metadata = self._load_analysis_snapshot_metadata(
                snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
            )
            self._verify_analysis_snapshot_metadata(
                snapshot,
                metadata,
                snapshot_root,
                require_active=True,
            )
            if not hmac.compare_digest(metadata.identity_digest, _digest(identity)) or not hmac.compare_digest(
                metadata.thread_digest,
                _digest(thread_id),
            ):
                raise CodingWorkspaceError("coding_analysis_identity_mismatch")
            if (
                workspace.workspace_ref != snapshot.workspace_ref
                or workspace.base_commit != snapshot.base_commit
                or workspace.repo_id != metadata.repo_id
                or not hmac.compare_digest(
                    metadata.workspace_digest,
                    _digest(workspace.workspace_ref),
                )
            ):
                raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
            self._require_analysis_workspace_scope(
                workspace,
                identity=identity,
                thread_id=thread_id,
            )
            return workspace.model_copy(
                update={
                    "root": snapshot_root / _ANALYSIS_SNAPSHOT_TREE_DIR,
                    "expires_at": snapshot.expires_at,
                }
            )

    def release_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> None:
        snapshot_root = self._analysis_snapshot_root(snapshot)
        with self._lock(snapshot.workspace_ref):
            metadata = self._load_analysis_snapshot_metadata(
                snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
            )
            self._verify_analysis_snapshot_metadata(
                snapshot,
                metadata,
                snapshot_root,
                require_active=False,
            )
            self._verify_analysis_snapshot_scope(
                snapshot,
                metadata,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
            )
            if metadata.active_lease:
                self._write_analysis_snapshot_metadata(
                    snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE,
                    metadata.model_copy(update={"active_lease": False}),
                )

    def list_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
        path: str,
        depth: int,
        cursor: int,
        limit: int,
    ) -> CodingListResult:
        return self.list_files(
            self.resolve_analysis_snapshot(
                snapshot,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
            ),
            path=path,
            depth=depth,
            cursor=cursor,
            limit=limit,
        )

    def search_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
        query: str,
        paths: tuple[str, ...],
        globs: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> CodingSearchResult:
        return self.search(
            self.resolve_analysis_snapshot(
                snapshot,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
            ),
            query=query,
            paths=paths,
            globs=globs,
            cursor=cursor,
            limit=limit,
        )

    def read_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        path: str,
        start_line: int,
        end_line: int,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> CodingReadResult:
        return self._read_file(
            self.resolve_analysis_snapshot(
                snapshot,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
            ),
            path,
            start_line=start_line,
            end_line=end_line,
            preserve_raw_newlines=True,
        )

    def status_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> CodingStatusResult:
        self.resolve_analysis_snapshot(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        )
        return self._analysis_snapshot_metadata(snapshot).status

    def diff_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> CodingDiffResult:
        self.resolve_analysis_snapshot(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        )
        return self._analysis_snapshot_metadata(snapshot).diff

    def list_files(
        self,
        workspace: CodingWorkspace,
        *,
        path: str,
        depth: int,
        cursor: int,
        limit: int,
    ) -> CodingListResult:
        if depth < 1 or depth > 8 or cursor < 0 or limit < 1 or limit > 200:
            raise CodingWorkspaceError("invalid_tool_input")
        start = self._read_path(workspace, path, allow_root=True)
        if not start.is_dir():
            raise CodingWorkspaceError("path_invalid")
        base_depth = len(start.relative_to(workspace.root).parts)
        entries: list[CodingListEntry] = []
        pending = [start]
        while pending:
            directory = pending.pop()
            for item in sorted(os.scandir(directory), key=lambda value: value.name):
                relative = Path(item.path).relative_to(workspace.root).as_posix()
                if relative == ".git" or relative.startswith(".git/") or item.is_symlink():
                    continue
                current_depth = len(Path(item.path).relative_to(workspace.root).parts) - base_depth
                if item.is_dir(follow_symlinks=False):
                    entries.append(CodingListEntry(path=relative, kind="directory"))
                    if current_depth < depth:
                        pending.append(Path(item.path))
                elif item.is_file(follow_symlinks=False):
                    entries.append(
                        CodingListEntry(
                            path=relative,
                            kind="file",
                            size_bytes=item.stat(follow_symlinks=False).st_size,
                        )
                    )
        entries.sort(key=lambda item: item.path)
        page = tuple(entries[cursor : cursor + limit])
        next_cursor = cursor + len(page) if cursor + len(page) < len(entries) else None
        return CodingListResult(entries=page, next_cursor=next_cursor)

    def search(
        self,
        workspace: CodingWorkspace,
        *,
        query: str,
        paths: tuple[str, ...],
        globs: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> CodingSearchResult:
        if not query or len(query) > 1_000 or cursor < 0 or limit < 1 or limit > 200:
            raise CodingWorkspaceError("invalid_tool_input")
        roots = [self._read_path(workspace, path, allow_root=True) for path in paths or ("",)]
        candidates: set[Path] = set()
        for root in roots:
            if root.is_file():
                candidates.add(root)
                continue
            for candidate in root.rglob("*"):
                if candidate.is_file() and not candidate.is_symlink():
                    candidates.add(candidate)
        matches: list[CodingSearchMatch] = []
        for candidate in sorted(candidates):
            relative = candidate.relative_to(workspace.root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                continue
            if globs and not any(fnmatchcase(relative, pattern) for pattern in globs):
                continue
            if candidate.stat().st_size > self.config.max_file_bytes:
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(
                        CodingSearchMatch(
                            path=relative,
                            line_number=line_number,
                            line=line[:2_000],
                        )
                    )
        page = tuple(matches[cursor : cursor + limit])
        next_cursor = cursor + len(page) if cursor + len(page) < len(matches) else None
        return CodingSearchResult(matches=page, next_cursor=next_cursor)

    def read(
        self,
        workspace: CodingWorkspace,
        path: str,
        *,
        start_line: int,
        end_line: int,
    ) -> CodingReadResult:
        return self._read_file(
            workspace,
            path,
            start_line=start_line,
            end_line=end_line,
            preserve_raw_newlines=False,
        )

    def _read_file(
        self,
        workspace: CodingWorkspace,
        path: str,
        *,
        start_line: int,
        end_line: int,
        preserve_raw_newlines: bool,
    ) -> CodingReadResult:
        if start_line < 1 or end_line < start_line or end_line - start_line > 2_000:
            raise CodingWorkspaceError("invalid_tool_input")
        candidate = self._read_path(workspace, path)
        if not candidate.is_file() or candidate.is_symlink():
            raise CodingWorkspaceError("path_invalid")
        if candidate.stat().st_size > self.config.max_file_bytes:
            raise CodingWorkspaceError("file_too_large")
        try:
            if preserve_raw_newlines:
                with candidate.open("r", encoding="utf-8", newline="") as handle:
                    lines = handle.readlines()
            else:
                lines = candidate.read_text(encoding="utf-8").splitlines(
                    keepends=True
                )
        except UnicodeDecodeError as exc:
            raise CodingWorkspaceError("file_encoding_unsupported") from exc
        except OSError as exc:
            raise CodingWorkspaceError("file_read_failed") from exc
        selected = lines[start_line - 1 : end_line]
        actual_end = start_line + len(selected) - 1 if selected else start_line - 1
        return CodingReadResult(
            path=Path(path).as_posix(),
            content="".join(selected),
            start_line=start_line,
            end_line=actual_end,
            total_lines=len(lines),
            next_line=actual_end + 1 if actual_end < len(lines) else None,
        )

    def status(self, workspace: CodingWorkspace) -> CodingStatusResult:
        output = self._run_git(
            workspace.root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_code="workspace_git_failed",
        )
        return CodingStatusResult(entries=tuple(output.splitlines()))

    def diff(self, workspace: CodingWorkspace) -> CodingDiffResult:
        output = self._run_git(
            workspace.root,
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--",
            error_code="workspace_git_failed",
        )
        truncated = len(output) > _MAX_GIT_OUTPUT
        return CodingDiffResult(diff=output[:_MAX_GIT_OUTPUT], truncated=truncated)

    def validate_patch(
        self,
        workspace: CodingWorkspace,
        patch: str,
        summary: str,
    ) -> CodingPatchValidation:
        try:
            parsed = parse_coding_patch(
                patch,
                policy=self.policy,
                root=workspace.root,
                limits=self.config,
            )
        except CodingPatchError as exc:
            raise CodingWorkspaceError(exc.code) from exc
        if not summary.strip() or len(summary) > 4_000:
            raise CodingWorkspaceError("invalid_tool_input")
        with self._lock(workspace.workspace_ref):
            self._require_base_commit(workspace)
            digests = self._base_file_digests(workspace, parsed.changed_paths)
            self._run_git(
                workspace.root,
                "apply",
                "--check",
                "--whitespace=nowarn",
                "-",
                error_code="patch_apply_conflict",
                input_text=patch,
            )
        proposal = CodingPatchProposal(
            patch=patch,
            summary=summary.strip(),
            changed_paths=parsed.changed_paths,
            base_commit=workspace.base_commit,
            base_file_digests=digests,
            patch_digest=parsed.patch_digest,
        )
        return CodingPatchValidation(
            proposal=proposal,
            diff_preview=patch[:32_000],
        )

    def apply_validated_patch(
        self,
        workspace: CodingWorkspace,
        validation: CodingPatchValidation,
    ) -> CodingPatchApplyResult:
        proposal = validation.proposal
        actual_digest = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
        if actual_digest != proposal.patch_digest:
            raise CodingWorkspaceError("approval_digest_mismatch")
        with self._lock(workspace.workspace_ref):
            self._require_base_commit(workspace)
            current_digests = self._base_file_digests(workspace, proposal.changed_paths)
            if current_digests != proposal.base_file_digests:
                raise CodingWorkspaceError("file_digest_changed")
            snapshots = {
                path: (
                    (workspace.root / path).read_bytes()
                    if (workspace.root / path).exists()
                    else None
                )
                for path in proposal.changed_paths
            }
            before_status = self.status(workspace)
            before_diff = self.diff(workspace)
            self._run_git(
                workspace.root,
                "apply",
                "--check",
                "--whitespace=nowarn",
                "-",
                error_code="patch_apply_conflict",
                input_text=proposal.patch,
            )
            try:
                self._run_git(
                    workspace.root,
                    "apply",
                    "--whitespace=nowarn",
                    "-",
                    error_code="patch_apply_failed",
                    input_text=proposal.patch,
                )
            except CodingWorkspaceError:
                self._restore_targets(workspace, snapshots)
                if self.status(workspace) != before_status or self.diff(workspace) != before_diff:
                    self._freeze_workspace(workspace.workspace_ref)
                    raise CodingWorkspaceError("rollback_failed")
                raise
            status = self.status(workspace)
            diff = self.diff(workspace)
        summary = "\n".join((*status.entries, diff.diff))[:32_000]
        return CodingPatchApplyResult(
            workspace_ref=workspace.workspace_ref,
            base_commit=workspace.base_commit,
            patch_digest=proposal.patch_digest,
            changed_paths=proposal.changed_paths,
            diff_summary=summary,
        )

    def preview_repair_patch(
        self,
        workspace: CodingWorkspace,
        validation: CodingPatchValidation,
        repair_round: int,
    ) -> CodingRepairApprovalContext:
        proposal = validation.proposal
        with self._lock(workspace.workspace_ref):
            actual_digest = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
            if actual_digest != proposal.patch_digest:
                raise CodingWorkspaceError("approval_digest_mismatch")
            if proposal.base_commit != workspace.base_commit:
                raise CodingWorkspaceError("base_commit_changed")
            self._require_base_commit(workspace)
            current_digests = self._base_file_digests(workspace, proposal.changed_paths)
            if current_digests != proposal.base_file_digests:
                raise CodingWorkspaceError("file_digest_changed")
            index_path = self._new_temporary_index(workspace.workspace_ref)
            git_env = {"GIT_INDEX_FILE": str(index_path)}
            try:
                self._run_git(
                    workspace.root,
                    "read-tree",
                    "HEAD",
                    error_code="repair_preview_failed",
                    extra_env=git_env,
                )
                self._run_git(
                    workspace.root,
                    "add",
                    "-A",
                    "--",
                    error_code="repair_preview_failed",
                    extra_env=git_env,
                )
                current_diff = self._run_git(
                    workspace.root,
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-color",
                    "HEAD",
                    "--",
                    error_code="repair_preview_failed",
                    extra_env=git_env,
                    max_output_chars=None,
                )
                self._run_git(
                    workspace.root,
                    "apply",
                    "--cached",
                    "--whitespace=nowarn",
                    "-",
                    error_code="repair_preview_failed",
                    input_text=proposal.patch,
                    extra_env=git_env,
                )
                candidate_diff = self._run_git(
                    workspace.root,
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-color",
                    "HEAD",
                    "--",
                    error_code="repair_preview_failed",
                    extra_env=git_env,
                    max_output_chars=None,
                )
            finally:
                self._remove_temporary_index(index_path)
        return CodingRepairApprovalContext(
            repair_round=repair_round,
            patch_digest=proposal.patch_digest,
            workspace_diff_digest=_digest(current_diff),
            candidate_diff_digest=_digest(candidate_diff),
            cumulative_diff_preview=candidate_diff[:32_000],
        )

    def _require_base_commit(self, workspace: CodingWorkspace) -> None:
        if self.git_head(workspace.root) != workspace.base_commit:
            raise CodingWorkspaceError("base_commit_changed")

    def _require_analysis_workspace_scope(
        self,
        workspace: CodingWorkspace,
        *,
        identity: str,
        thread_id: str,
    ) -> None:
        try:
            current = self.get(
                workspace.workspace_ref,
                identity=identity,
                thread_id=thread_id,
            )
        except CodingWorkspaceError as exc:
            if exc.code == "workspace_identity_mismatch":
                raise CodingWorkspaceError(
                    "coding_analysis_identity_mismatch"
                ) from exc
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch") from exc
        if (
            current.workspace_ref != workspace.workspace_ref
            or current.root != workspace.root
            or current.repo_id != workspace.repo_id
            or current.base_commit != workspace.base_commit
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")

    def _analysis_snapshot_workspace(
        self,
        snapshot: CodingAnalysisSnapshot,
    ) -> CodingWorkspace:
        metadata = self._analysis_snapshot_metadata(snapshot)
        return CodingWorkspace(
            workspace_ref=snapshot.workspace_ref,
            root=self._analysis_snapshot_root(snapshot) / _ANALYSIS_SNAPSHOT_TREE_DIR,
            repo_id=metadata.repo_id,
            base_commit=snapshot.base_commit,
            expires_at=snapshot.expires_at,
        )

    def _verify_analysis_snapshot_scope(
        self,
        snapshot: CodingAnalysisSnapshot,
        metadata: _AnalysisSnapshotMetadata,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> None:
        if not hmac.compare_digest(metadata.identity_digest, _digest(identity)) or not hmac.compare_digest(
            metadata.thread_digest,
            _digest(thread_id),
        ):
            raise CodingWorkspaceError("coding_analysis_identity_mismatch")
        if (
            workspace.workspace_ref != snapshot.workspace_ref
            or workspace.base_commit != snapshot.base_commit
            or workspace.repo_id != metadata.repo_id
            or not hmac.compare_digest(
                metadata.workspace_digest,
                _digest(workspace.workspace_ref),
            )
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
        self._require_analysis_workspace_scope(
            workspace,
            identity=identity,
            thread_id=thread_id,
        )

    def _analysis_snapshot_metadata(
        self,
        snapshot: CodingAnalysisSnapshot,
    ) -> _AnalysisSnapshotMetadata:
        snapshot_root = self._analysis_snapshot_root(snapshot)
        metadata = self._load_analysis_snapshot_metadata(
            snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
        )
        self._verify_analysis_snapshot_metadata(snapshot, metadata, snapshot_root)
        return metadata

    def _analysis_snapshot_ref(
        self,
        *,
        identity_digest: str,
        thread_digest: str,
        workspace_ref: str,
        tree_object: str,
        workspace_diff_digest: str,
    ) -> str:
        payload = "\0".join(
            (
                "coding-analysis-snapshot-v1",
                identity_digest,
                thread_digest,
                workspace_ref,
                tree_object,
                workspace_diff_digest,
            )
        ).encode("utf-8")
        return hmac.new(self._secret(), payload, hashlib.sha256).hexdigest()

    def _analysis_snapshot_root(self, snapshot: CodingAnalysisSnapshot) -> Path:
        snapshot_ref = snapshot.snapshot_ref
        if len(snapshot_ref) != 64 or any(
            character not in "0123456789abcdef" for character in snapshot_ref
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
        return (
            self._management_root(snapshot.workspace_ref)
            / _ANALYSIS_SNAPSHOTS_DIR
            / snapshot_ref
        )

    def _load_analysis_snapshot_metadata(
        self,
        path: Path,
    ) -> _AnalysisSnapshotMetadata:
        try:
            return _AnalysisSnapshotMetadata.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch") from exc

    def _write_analysis_snapshot_metadata(
        self,
        path: Path,
        metadata: _AnalysisSnapshotMetadata,
    ) -> None:
        payload = metadata.model_dump_json().encode("utf-8")
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=_ANALYSIS_METADATA_TEMP_PREFIX,
                dir=path.parent,
            )
            temporary_path = Path(raw_path)
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("metadata write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_path, path)
            temporary_path = None
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path.parent, flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except (OSError, ValueError) as exc:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc

    def _verify_analysis_snapshot_metadata(
        self,
        snapshot: CodingAnalysisSnapshot,
        metadata: _AnalysisSnapshotMetadata,
        snapshot_root: Path,
        *,
        require_active: bool = True,
    ) -> None:
        expected_ref = self._analysis_snapshot_ref(
            identity_digest=metadata.identity_digest,
            thread_digest=metadata.thread_digest,
            workspace_ref=snapshot.workspace_ref,
            tree_object=metadata.tree_object,
            workspace_diff_digest=snapshot.workspace_diff_digest,
        )
        if (
            metadata.snapshot != snapshot
            or not hmac.compare_digest(
                metadata.workspace_digest,
                _digest(snapshot.workspace_ref),
            )
            or not hmac.compare_digest(snapshot.snapshot_ref, expected_ref)
            or not hmac.compare_digest(
                snapshot.tree_digest,
                _digest(metadata.tree_object),
            )
            or not (snapshot_root / _ANALYSIS_SNAPSHOT_TREE_DIR).is_dir()
            or (require_active and not metadata.active_lease)
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
        if snapshot.expires_at <= self._clock():
            raise CodingWorkspaceError("coding_analysis_snapshot_expired")

    def _normalize_analysis_index(
        self,
        workspace: CodingWorkspace,
        git_env: Mapping[str, str],
    ) -> dict[str, tuple[str, bytes]]:
        raw_entries = self._run_git_bytes(
            workspace.root,
            "ls-files",
            "--stage",
            "-z",
            error_code="coding_analysis_snapshot_failed",
            extra_env=git_env,
            max_output_bytes=(
                self.config.analysis_snapshot_max_files * (1_024 + 128)
            ),
        )
        files: dict[str, tuple[str, bytes]] = {}
        removed_paths: list[bytes] = []
        blob_cache: dict[str, bytes | None] = {}
        for raw_entry in raw_entries.split(b"\0"):
            if not raw_entry:
                continue
            try:
                raw_metadata, raw_path = raw_entry.split(b"\t", 1)
                mode_bytes, object_bytes, stage_bytes = raw_metadata.split(b" ", 2)
                mode = mode_bytes.decode("ascii")
                object_id = object_bytes.decode("ascii")
                stage = stage_bytes.decode("ascii")
                path = raw_path.decode("utf-8")
            except (UnicodeDecodeError, ValueError) as exc:
                raise CodingWorkspaceError(
                    "coding_analysis_snapshot_failed"
                ) from exc
            if stage != "0":
                raise CodingWorkspaceError("coding_analysis_snapshot_failed")
            if not self._analysis_path_is_allowed(workspace.root, path, mode):
                removed_paths.append(raw_path)
                continue
            if object_id not in blob_cache:
                blob_cache[object_id] = self._read_analysis_git_blob(
                    workspace.root,
                    object_id,
                    git_env,
                )
            blob = blob_cache[object_id]
            if blob is None:
                removed_paths.append(raw_path)
                continue
            files[path] = (mode, blob)
            self._enforce_analysis_tree_limits(files)

        if removed_paths:
            self._run_git_bytes(
                workspace.root,
                "update-index",
                "--force-remove",
                "-z",
                "--stdin",
                error_code="coding_analysis_snapshot_failed",
                extra_env=git_env,
                input_bytes=b"\0".join(removed_paths) + b"\0",
            )
        self._enforce_analysis_tree_limits(files)
        return files

    def _analysis_path_is_allowed(self, root: Path, path: str, mode: str) -> bool:
        if len(path.encode("utf-8")) > 1_024 or mode not in {"100644", "100755"} or any(
            character in path for character in ("\x00", "\n", "\r")
        ):
            return False
        try:
            self.policy.validate_relative_path(root, path, operation="read")
        except CodingPolicyError:
            return False
        return True

    def _read_analysis_git_blob(
        self,
        repo: Path,
        object_id: str,
        git_env: Mapping[str, str],
    ) -> bytes | None:
        try:
            size = int(
                self._run_git(
                    repo,
                    "cat-file",
                    "-s",
                    object_id,
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                ).strip()
            )
        except ValueError as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
        if size > self.config.max_file_bytes:
            return None
        data = self._run_git_bytes(
            repo,
            "cat-file",
            "blob",
            object_id,
            error_code="coding_analysis_snapshot_failed",
            extra_env=git_env,
            max_output_bytes=self.config.max_file_bytes,
        )
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return data

    def _build_analysis_worktree_index(
        self,
        workspace: CodingWorkspace,
        git_env: Mapping[str, str],
    ) -> dict[str, tuple[str, bytes]]:
        files: dict[str, tuple[str, bytes]] = {}
        index_records: list[bytes] = []
        for directory, child_directories, filenames in os.walk(
            workspace.root,
            followlinks=False,
        ):
            root = Path(directory)
            child_directories[:] = [
                name
                for name in child_directories
                if not (root / name).is_symlink()
                and (root / name).name != ".git"
            ]
            for filename in filenames:
                candidate = root / filename
                path = candidate.relative_to(workspace.root).as_posix()
                mode = "100755" if os.access(candidate, os.X_OK) else "100644"
                if not self._analysis_path_is_allowed(workspace.root, path, mode):
                    continue
                data = self._read_analysis_worktree_file(candidate)
                if data is None:
                    continue
                files[path] = (mode, data)
                self._enforce_analysis_tree_limits(files)
                object_id = self._run_git_bytes(
                    workspace.root,
                    "hash-object",
                    "-w",
                    "--stdin",
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                    input_bytes=data,
                    max_output_bytes=128,
                ).decode("ascii").strip()
                if len(object_id) not in {40, 64} or any(
                    character not in "0123456789abcdef" for character in object_id
                ):
                    raise CodingWorkspaceError("coding_analysis_snapshot_failed")
                index_records.append(
                    f"{mode} {object_id}\t{path}".encode("utf-8") + b"\0"
                )
        if index_records:
            self._run_git_bytes(
                workspace.root,
                "update-index",
                "-z",
                "--index-info",
                error_code="coding_analysis_snapshot_failed",
                extra_env=git_env,
                input_bytes=b"".join(index_records),
                max_output_bytes=1,
            )
        return files

    def _read_analysis_worktree_file(self, path: Path) -> bytes | None:
        descriptor = -1
        try:
            initial = path.lstat()
            if not stat.S_ISREG(initial.st_mode):
                return None
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > self.config.max_file_bytes:
                return None
            chunks: list[bytes] = []
            remaining = self.config.max_file_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > self.config.max_file_bytes:
                return None
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                return None
            return data
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _enforce_analysis_tree_limits(
        self,
        files: Mapping[str, tuple[str, bytes]],
    ) -> None:
        if len(files) > self.config.analysis_snapshot_max_files or sum(
            len(item[1]) for item in files.values()
        ) > self.config.analysis_snapshot_max_total_bytes:
            raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded")

    def _analysis_git_environment(
        self,
        workspace: CodingWorkspace,
        *,
        index_path: Path,
        object_root: Path,
    ) -> dict[str, str]:
        raw_common = self._run_git(
            workspace.root,
            "rev-parse",
            "--git-common-dir",
            error_code="coding_analysis_snapshot_failed",
        ).strip()
        common = Path(raw_common)
        if not common.is_absolute():
            common = (workspace.root / common).resolve()
        main_objects = (common / "objects").resolve()
        if not main_objects.is_dir():
            raise CodingWorkspaceError("coding_analysis_snapshot_failed")
        return {
            "GIT_INDEX_FILE": str(index_path),
            "GIT_OBJECT_DIRECTORY": str(object_root),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(main_objects),
        }

    def _analysis_git_diff_result(
        self,
        workspace: CodingWorkspace,
        baseline_tree_object: str,
        current_tree_object: str,
        git_env: Mapping[str, str],
    ) -> tuple[CodingDiffResult, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EXTERNAL_DIFF": "",
            **git_env,
        }
        command = (
            "git",
            "-C",
            str(workspace.root),
            "-c",
            "diff.external=",
            "-c",
            f"core.attributesFile={os.devnull}",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            baseline_tree_object,
            current_tree_object,
            "--",
        )
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        digest = hashlib.sha256()
        preview = bytearray()
        total_bytes = 0
        limit = self.config.analysis_snapshot_max_diff_bytes
        deadline = time.monotonic() + 20.0
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
            if process.stdout is None:
                raise CodingWorkspaceError("coding_analysis_snapshot_failed")
            descriptor = process.stdout.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
            while selector.get_map():
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise subprocess.TimeoutExpired(command, 20.0)
                events = selector.select(remaining_seconds)
                if not events:
                    raise subprocess.TimeoutExpired(command, 20.0)
                for key, _ in events:
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    digest.update(chunk)
                    total_bytes += len(chunk)
                    if len(preview) < limit:
                        preview.extend(chunk[: limit - len(preview)])
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise subprocess.TimeoutExpired(command, 20.0)
            if process.wait(timeout=remaining_seconds) != 0:
                raise CodingWorkspaceError("coding_analysis_snapshot_failed")
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    try:
                        process.wait(timeout=1)
                    except subprocess.SubprocessError:
                        pass
                if process.stdout is not None:
                    process.stdout.close()
        while preview:
            try:
                preview_text = preview.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                if exc.end != len(preview):
                    raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
                del preview[exc.start:]
        else:
            preview_text = ""
        return (
            CodingDiffResult(
                diff=preview_text,
                truncated=total_bytes > len(preview),
            ),
            digest.hexdigest(),
        )

    def _materialize_analysis_files(
        self,
        tree_root: Path,
        files: Mapping[str, tuple[str, bytes]],
    ) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            for relative_path in sorted(files):
                mode, content = files[relative_path]
                candidate = tree_root / relative_path
                candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
                    0o700 if mode == "100755" else 0o600,
                )
                try:
                    remaining = memoryview(content)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("snapshot write made no progress")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError) as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc

    def _analysis_status_result(
        self,
        baseline: Mapping[str, tuple[str, bytes]],
        current: Mapping[str, tuple[str, bytes]],
    ) -> CodingStatusResult:
        entries: list[str] = []
        total_bytes = 0
        for path in sorted(set(baseline).union(current)):
            old = baseline.get(path)
            new = current.get(path)
            if old == new:
                continue
            if old is None:
                prefix = "??"
            elif new is None:
                prefix = " D"
            else:
                prefix = " M"
            entry = f"{prefix} {path}"
            entries.append(entry)
            total_bytes += len(entry.encode("utf-8"))
            if (
                len(entries) > self.config.analysis_snapshot_max_status_entries
                or total_bytes > self.config.analysis_snapshot_max_status_bytes
            ):
                raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded")
        return CodingStatusResult(entries=tuple(entries))

    def _cleanup_analysis_build_resources(
        self,
        *,
        index_path: Path | None,
        object_root: Path | None,
        temporary_root: Path | None,
    ) -> bool:
        failed = False
        if index_path is not None:
            try:
                index_path.unlink(missing_ok=True)
                Path(f"{index_path}.lock").unlink(missing_ok=True)
            except OSError:
                failed = True
        if object_root is not None:
            try:
                shutil.rmtree(object_root, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError:
                failed = True
        if temporary_root is not None:
            try:
                self._remove_analysis_snapshot_directory(temporary_root)
            except OSError:
                failed = True
        return failed

    def _deactivate_analysis_snapshot_best_effort(self, snapshot_root: Path) -> None:
        try:
            path = snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
            metadata = self._load_analysis_snapshot_metadata(path)
            self._write_analysis_snapshot_metadata(
                path,
                metadata.model_copy(update={"active_lease": False}),
            )
        except CodingWorkspaceError:
            return

    def _remove_analysis_index(self, index_path: Path) -> None:
        try:
            index_path.unlink(missing_ok=True)
            Path(f"{index_path}.lock").unlink(missing_ok=True)
        except OSError as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc

    def _make_analysis_tree_read_only(self, tree_root: Path) -> None:
        try:
            for directory, child_directories, filenames in os.walk(tree_root):
                root = Path(directory)
                child_directories[:] = [
                    name
                    for name in child_directories
                    if not (root / name).is_symlink()
                ]
                for filename in filenames:
                    candidate = root / filename
                    if candidate.is_symlink():
                        continue
                    candidate.chmod(candidate.stat().st_mode & 0o555)
                root.chmod(root.stat().st_mode & 0o555)
        except OSError as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc

    def _remove_analysis_snapshot_directory(self, path: Path) -> None:
        if not path.exists():
            return
        for directory, child_directories, _ in os.walk(path):
            root = Path(directory)
            child_directories[:] = [
                name for name in child_directories if not (root / name).is_symlink()
            ]
            root.chmod(0o700)
        shutil.rmtree(path, ignore_errors=False)

    def _base_file_digests(
        self,
        workspace: CodingWorkspace,
        paths: tuple[str, ...],
    ) -> dict[str, str]:
        digests: dict[str, str] = {}
        for path in paths:
            try:
                candidate = self.policy.validate_relative_path(
                    workspace.root,
                    path,
                    operation="write",
                )
            except CodingPolicyError as exc:
                raise CodingWorkspaceError(exc.code) from exc
            if not candidate.exists():
                digests[path] = "absent"
                continue
            if not candidate.is_file() or candidate.is_symlink():
                raise CodingWorkspaceError("path_invalid")
            data = candidate.read_bytes()
            if len(data) > self.config.max_file_bytes:
                raise CodingWorkspaceError("file_too_large")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CodingWorkspaceError("file_encoding_unsupported") from exc
            digests[path] = hashlib.sha256(data).hexdigest()
        return digests

    def _restore_targets(
        self,
        workspace: CodingWorkspace,
        snapshots: dict[str, bytes | None],
    ) -> None:
        for path, snapshot in snapshots.items():
            candidate = workspace.root / path
            if snapshot is None:
                candidate.unlink(missing_ok=True)
                parent = candidate.parent
                while parent != workspace.root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            else:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(snapshot)

    def _freeze_workspace(self, workspace_ref: str) -> None:
        path = self._management_root(workspace_ref) / _METADATA_FILE
        metadata = self._load_metadata(path)
        self._write_metadata(path, metadata.model_copy(update={"frozen": True}))

    def _new_temporary_index(self, workspace_ref: str) -> Path:
        management_root = self._management_root(workspace_ref)
        descriptor = -1
        path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".repair-index-",
                dir=management_root,
            )
            path = Path(raw_path)
            os.close(descriptor)
            descriptor = -1
            path.unlink()
        except OSError as exc:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise CodingWorkspaceError("repair_preview_cleanup_failed") from exc
        return path

    def _remove_temporary_index(self, index_path: Path) -> None:
        try:
            index_path.unlink(missing_ok=True)
            Path(f"{index_path}.lock").unlink(missing_ok=True)
        except OSError as exc:
            raise CodingWorkspaceError("repair_preview_cleanup_failed") from exc

    def cleanup_expired(self) -> None:
        root = self.config.workspace_root
        if not root.is_dir():
            return
        for management_root in tuple(root.iterdir()):
            metadata_path = management_root / _METADATA_FILE
            if not management_root.is_dir() or not metadata_path.is_file():
                continue
            try:
                metadata = self._load_metadata(metadata_path)
            except CodingWorkspaceError:
                continue
            lock_path = management_root / _LOCK_FILE
            lock_path.touch(mode=0o600, exist_ok=True)
            remove_workspace = False
            with lock_path.open("a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                self._reap_analysis_snapshots_locked(management_root)
                if metadata.expires_at <= self._clock():
                    repository = self.config.repositories.get(metadata.repo_id)
                    if repository is None:
                        continue
                    self._run_git(
                        repository.path,
                        "worktree",
                        "remove",
                        "--force",
                        str(management_root / _REPO_DIR),
                        error_code="workspace_cleanup_failed",
                    )
                    remove_workspace = True
            if remove_workspace:
                shutil.rmtree(management_root, ignore_errors=False)

    def _reap_analysis_snapshots_locked(self, management_root: Path) -> None:
        snapshots_root = management_root / _ANALYSIS_SNAPSHOTS_DIR
        if snapshots_root.is_dir():
            for snapshot_root in tuple(snapshots_root.iterdir()):
                if not snapshot_root.is_dir():
                    continue
                if snapshot_root.name.startswith(_ANALYSIS_BUILD_PREFIX):
                    try:
                        self._remove_analysis_snapshot_directory(snapshot_root)
                    except OSError:
                        continue
                    continue
                if snapshot_root.name.startswith(_ANALYSIS_QUARANTINE_PREFIX):
                    try:
                        quarantined_at = datetime.fromtimestamp(
                            snapshot_root.stat(follow_symlinks=False).st_mtime,
                            timezone.utc,
                        )
                        reclaim_at = quarantined_at + timedelta(
                            seconds=_ANALYSIS_QUARANTINE_SECONDS
                        )
                        if reclaim_at <= self._clock():
                            self._remove_analysis_snapshot_directory(snapshot_root)
                    except OSError:
                        continue
                    continue
                try:
                    metadata = self._load_analysis_snapshot_metadata(
                        snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
                    )
                except CodingWorkspaceError:
                    self._quarantine_analysis_snapshot(snapshot_root)
                    continue
                if metadata.snapshot.expires_at > self._clock():
                    continue
                try:
                    self._remove_analysis_snapshot_directory(snapshot_root)
                except OSError:
                    continue
        for index_path in management_root.glob(".analysis-index-*"):
            try:
                index_path.unlink(missing_ok=True)
                Path(f"{index_path}.lock").unlink(missing_ok=True)
            except OSError:
                continue

    def _quarantine_analysis_snapshot(self, snapshot_root: Path) -> None:
        quarantine_root = snapshot_root.with_name(
            f"{_ANALYSIS_QUARANTINE_PREFIX}{snapshot_root.name}-{secrets.token_hex(8)}"
        )
        try:
            os.replace(snapshot_root, quarantine_root)
            timestamp = self._clock().timestamp()
            os.utime(
                quarantine_root,
                times=(timestamp, timestamp),
                follow_symlinks=False,
            )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(quarantine_root.parent, flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            return

    async def aclose(self) -> None:
        return None

    def _read_path(
        self,
        workspace: CodingWorkspace,
        path: str,
        *,
        allow_root: bool = False,
    ) -> Path:
        if allow_root and str(path).strip() in {"", "."}:
            return workspace.root
        try:
            return self.policy.validate_relative_path(
                workspace.root,
                path,
                operation="read",
            )
        except CodingPolicyError as exc:
            raise CodingWorkspaceError(exc.code) from exc

    def _workspace_ref(self, identity: str, thread_id: str, repo_id: str) -> str:
        payload = "\0".join((identity, thread_id, repo_id)).encode("utf-8")
        return hmac.new(self._secret(), payload, hashlib.sha256).hexdigest()

    def _secret(self) -> bytes:
        if self._cached_secret is not None:
            return self._cached_secret
        self.config.workspace_root.mkdir(parents=True, exist_ok=True)
        secret_path = self.config.workspace_root / _SECRET_FILE
        try:
            descriptor = os.open(
                secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            value = secret_path.read_bytes()
        else:
            value = secrets.token_bytes(32)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
        if len(value) < 32:
            raise CodingWorkspaceError("workspace_secret_invalid")
        self._cached_secret = value
        return value

    def _management_root(self, workspace_ref: str) -> Path:
        if len(workspace_ref) != 64 or any(character not in "0123456789abcdef" for character in workspace_ref):
            raise CodingWorkspaceError("workspace_not_allowed")
        return self.config.workspace_root / workspace_ref

    @contextmanager
    def _lock(self, workspace_ref: str) -> Iterator[None]:
        lock_path = self._management_root(workspace_ref) / _LOCK_FILE
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _workspace(self, metadata: CodingWorkspaceMetadata) -> CodingWorkspace:
        return CodingWorkspace(
            workspace_ref=metadata.workspace_ref,
            root=self._management_root(metadata.workspace_ref) / _REPO_DIR,
            repo_id=metadata.repo_id,
            base_commit=metadata.base_commit,
            expires_at=metadata.expires_at,
        )

    def _verify_scope(
        self,
        metadata: CodingWorkspaceMetadata,
        *,
        identity: str,
        thread_id: str,
    ) -> None:
        if not hmac.compare_digest(metadata.identity_digest, _digest(identity)) or not hmac.compare_digest(
            metadata.thread_digest,
            _digest(thread_id),
        ):
            raise CodingWorkspaceError("workspace_identity_mismatch")

    def _load_metadata(self, path: Path) -> CodingWorkspaceMetadata:
        try:
            return CodingWorkspaceMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CodingWorkspaceError("workspace_metadata_invalid") from exc

    def _write_metadata(self, path: Path, metadata: CodingWorkspaceMetadata) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(metadata.model_dump_json(), encoding="utf-8")
        os.replace(temporary, path)

    def _run_git(
        self,
        repo: Path,
        *args: str,
        error_code: str,
        input_text: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        max_output_chars: int | None = _MAX_GIT_OUTPUT,
    ) -> str:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if extra_env is not None:
            env.update(extra_env)
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodingWorkspaceError(error_code) from exc
        if completed.returncode != 0:
            raise CodingWorkspaceError(error_code)
        if max_output_chars is not None and len(completed.stdout) > max_output_chars:
            raise CodingWorkspaceError("workspace_output_too_large")
        return completed.stdout

    def _run_git_bytes(
        self,
        repo: Path,
        *args: str,
        error_code: str,
        extra_env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        max_output_bytes: int | None = None,
    ) -> bytes:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if extra_env is not None:
            env.update(extra_env)
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                input=input_bytes,
                capture_output=True,
                timeout=20,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodingWorkspaceError(error_code) from exc
        if completed.returncode != 0:
            raise CodingWorkspaceError(error_code)
        if max_output_bytes is not None and len(completed.stdout) > max_output_bytes:
            raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded")
        return completed.stdout


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["CodingWorkspaceError", "CodingWorkspaceService"]
