"""Thread-scoped Git worktrees and governed read operations."""

from __future__ import annotations

import asyncio
import json
import ctypes
import fcntl
import hashlib
import hmac
import os
import selectors
import secrets
import shutil
import stat
import subprocess
import struct
import tempfile
import time
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Mapping, Sequence
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
_REAPER_CURSOR_LIMIT = 4
_REAPER_TOMBSTONE_PREFIX = ".assistant-agent-reap-"
_ADMIN_TOMBSTONE_AREA = ".assistant-agent-worktree-tombstones"
_ADMIN_TOMBSTONE_PREFIX = ".assistant-agent-admin-reap-"
_ADMIN_REAPER_PENDING_FILE = ".admin-cleanup-pending"
_ADMIN_VALIDATION_ENTRY_LIMIT = 512
_ADMIN_VALIDATION_DEPTH_LIMIT = 16
_ADMIN_DISCOVERY_ENTRY_BUDGET = 8
_ADMIN_REAPER_TIME_SLICE_SECONDS = 0.025
_REAPER_PROGRESS_FILE = ".cleanup-progress.json"
_REAPER_TIME_BUDGET_SECONDS = 0.25
_REAPER_MAX_TIME_BUDGET_SECONDS = 1.0
_REAPER_DEFAULT_WORKSPACE_BUDGET = 32
_REAPER_DEFAULT_CHILD_BUDGET = 64
_ANALYSIS_INDEX_ENTRY_MAX_BYTES = 1_152


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


@dataclass(slots=True)
class _AnalysisScanBudget:
    config: CodingConfig
    visited_entries: int = 0
    visited_directories: int = 0
    attempted_bytes: int = 0
    read_bytes: int = 0
    included_files: int = 0
    included_bytes: int = 0

    def visit_entry(self) -> None:
        self.visited_entries += 1
        self._enforce()

    def visit_directory(self) -> None:
        self.visited_directories += 1
        self._enforce()

    def attempt_file(self, size: int) -> None:
        self.attempted_bytes += max(0, size)
        self._enforce()

    def consume_read(self, size: int) -> None:
        self.read_bytes += max(0, size)
        self._enforce()

    def include_file(self, size: int) -> None:
        self.included_files += 1
        self.included_bytes += max(0, size)
        self._enforce()

    def _enforce(self) -> None:
        if (
            self.visited_entries > self.config.analysis_snapshot_max_scan_entries
            or self.visited_directories
            > self.config.analysis_snapshot_max_scan_directories
            or self.attempted_bytes > self.config.analysis_snapshot_max_scan_bytes
            or self.read_bytes > self.config.analysis_snapshot_max_scan_bytes
            or self.included_files > self.config.analysis_snapshot_max_files
            or self.included_bytes > self.config.analysis_snapshot_max_total_bytes
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded")


@dataclass(slots=True)
class _ReaperCursor:
    path: Path
    device: int
    inode: int
    iterator: Iterator[os.DirEntry[str]]

    def close(self) -> None:
        closer = getattr(self.iterator, "close", None)
        if callable(closer):
            closer()


@dataclass(slots=True)
class _ValidatedWorktreeAdmin:
    common_root: Path
    common_fd: int
    worktrees_fd: int
    admin_fd: int
    admin_name: str

    def close(self) -> None:
        for descriptor in (self.admin_fd, self.worktrees_fd, self.common_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


class CodingWorkspaceService:
    _REAPER_CURSOR_LIMIT = _REAPER_CURSOR_LIMIT

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
        self._snapshot_reaper_task: asyncio.Task[None] | None = None
        self._reaper_cursor_lock = threading.RLock()
        self._reaper_cursors: OrderedDict[str, _ReaperCursor] = OrderedDict()
        self._hierarchical_reaper = _HierarchicalReaperTraversal(
            self.config.workspace_root,
            child_directories=(("snapshot", _ANALYSIS_SNAPSHOTS_DIR), ("management", ".")),
        )
        self._incremental_reaper_deletion: _IncrementalTombstoneDeletion | None = None
        self._incremental_admin_deletion: _IncrementalTombstoneDeletion | None = None
        self._admin_repository_cursor = 0
        self._admin_empty_repository_scans = 0

    def resolve(self, identity: str, thread_id: str, repo_id: str) -> CodingWorkspace:
        if not self.config.enabled or repo_id not in self.config.repositories:
            raise CodingWorkspaceError("workspace_not_allowed")
        if not identity.strip() or not thread_id.strip():
            raise CodingWorkspaceError("workspace_identity_mismatch")
        self.cleanup_expired(max_workspaces=32, max_snapshots_per_workspace=64)
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
                baseline_files = self._normalize_analysis_index(
                    workspace,
                    git_env,
                    _AnalysisScanBudget(self.config),
                )
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
                    _AnalysisScanBudget(self.config),
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

    def validate_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
        require_active: bool,
    ) -> None:
        """Validate a checkpoint binding without recreating or renewing it."""

        snapshot_root = self._analysis_snapshot_root(snapshot)
        with self._lock(snapshot.workspace_ref):
            metadata = self._load_analysis_snapshot_metadata(
                snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
            )
            self._verify_analysis_snapshot_metadata(
                snapshot,
                metadata,
                snapshot_root,
                require_active=require_active,
            )
            self._verify_analysis_snapshot_scope(
                snapshot,
                metadata,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
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
        budget: _AnalysisScanBudget,
    ) -> dict[str, tuple[str, bytes]]:
        files: dict[str, tuple[str, bytes]] = {}
        blob_cache: dict[str, bytes | None] = {}
        governed_env = _governed_git_environment(git_env)
        command = ("git", "-C", str(workspace.root), "ls-files", "--stage", "-z")
        with tempfile.TemporaryFile() as removed_paths:
            removed_count = 0

            def consume_record(_size: int) -> None:
                budget.visit_entry()

            try:
                records = _iter_git_nul_records_process(
                    command,
                    cwd=workspace.root,
                    env=governed_env,
                    timeout_seconds=20.0,
                    max_record_bytes=_ANALYSIS_INDEX_ENTRY_MAX_BYTES,
                    consume_record=consume_record,
                    consume_bytes=budget.consume_read,
                )
                for raw_entry in records:
                    try:
                        raw_metadata, raw_path = raw_entry.split(b"\t", 1)
                        mode_bytes, object_bytes, stage_bytes = raw_metadata.split(b" ", 2)
                        mode = mode_bytes.decode("ascii")
                        object_id = object_bytes.decode("ascii")
                        stage = stage_bytes.decode("ascii")
                        path = raw_path.decode("utf-8")
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
                    if stage != "0":
                        raise CodingWorkspaceError("coding_analysis_snapshot_failed")
                    if not self._analysis_path_is_allowed(workspace.root, path, mode):
                        removed_paths.write(raw_path)
                        removed_paths.write(b"\0")
                        removed_count += 1
                        continue
                    if object_id not in blob_cache:
                        blob_cache[object_id] = self._read_analysis_git_blob(
                            workspace.root,
                            object_id,
                            git_env,
                            budget,
                        )
                    blob = blob_cache[object_id]
                    if blob is None:
                        removed_paths.write(raw_path)
                        removed_paths.write(b"\0")
                        removed_count += 1
                        continue
                    files[path] = (mode, blob)
                    budget.include_file(len(blob))
            except CodingWorkspaceError:
                raise
            except (OSError, subprocess.TimeoutExpired, _GitProcessFailed) as exc:
                raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
            if removed_count:
                removed_paths.flush()
                removed_paths.seek(0)
                self._run_git_stream_input(
                    workspace.root,
                    "update-index",
                    "--force-remove",
                    "-z",
                    "--stdin",
                    error_code="coding_analysis_snapshot_failed",
                    extra_env=git_env,
                    input_file=removed_paths,
                )
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
        budget: _AnalysisScanBudget,
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
        budget.attempt_file(size)
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
        budget.consume_read(len(data))
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return data

    def _build_analysis_worktree_index(
        self,
        workspace: CodingWorkspace,
        git_env: Mapping[str, str],
        budget: _AnalysisScanBudget,
    ) -> dict[str, tuple[str, bytes]]:
        files: dict[str, tuple[str, bytes]] = {}
        index_records: list[bytes] = []
        budget.visit_directory()
        pending_directories = [workspace.root]
        while pending_directories:
            root = pending_directories.pop()
            try:
                entries = os.scandir(root)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    budget.visit_entry()
                    candidate = Path(entry.path)
                    path = candidate.relative_to(workspace.root).as_posix()
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    budget.attempt_file(details.st_size)
                    if stat.S_ISDIR(details.st_mode):
                        budget.visit_directory()
                        if candidate.name == ".git":
                            continue
                        try:
                            self.policy.validate_relative_path(
                                workspace.root,
                                path,
                                operation="read",
                            )
                        except CodingPolicyError:
                            continue
                        pending_directories.append(candidate)
                        continue
                    if not stat.S_ISREG(details.st_mode):
                        continue
                    mode = (
                        "100755"
                        if details.st_mode
                        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        else "100644"
                    )
                    if not self._analysis_path_is_allowed(
                        workspace.root,
                        path,
                        mode,
                    ):
                        continue
                    data = self._read_analysis_worktree_file(
                        candidate,
                        details,
                        budget,
                    )
                    if data is None:
                        continue
                    files[path] = (mode, data)
                    budget.include_file(len(data))
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
                        character not in "0123456789abcdef"
                        for character in object_id
                    ):
                        raise CodingWorkspaceError(
                            "coding_analysis_snapshot_failed"
                        )
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

    def _read_analysis_worktree_file(
        self,
        path: Path,
        initial: os.stat_result,
        budget: _AnalysisScanBudget,
    ) -> bytes | None:
        descriptor = -1
        try:
            if not stat.S_ISREG(initial.st_mode):
                return None
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_dev != initial.st_dev
                or details.st_ino != initial.st_ino
                or details.st_size != initial.st_size
                or details.st_size > self.config.max_file_bytes
            ):
                return None
            chunks: list[bytes] = []
            remaining = self.config.max_file_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                budget.consume_read(len(chunk))
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
        governed_env = _governed_git_environment()
        object_format = _detect_git_object_format(workspace.root, governed_env)
        isolated_git_dir = object_root / ".canonical-git"
        _initialize_isolated_git_dir(isolated_git_dir, object_format)
        return {
            "GIT_INDEX_FILE": str(index_path),
            "GIT_OBJECT_DIRECTORY": str(object_root),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(main_objects),
            "ASSISTANT_AGENT_ANALYSIS_GIT_DIR": str(isolated_git_dir),
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
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_EXTERNAL_DIFF": "",
            **git_env,
        }
        environment["GIT_DIR"] = environment.pop(
            "ASSISTANT_AGENT_ANALYSIS_GIT_DIR"
        )
        command = (
            "git",
            "-C",
            str(workspace.root),
            "-c",
            "diff.external=",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.abbrev=40",
            "-c",
            "core.quotePath=true",
            "-c",
            "diff.algorithm=myers",
            "-c",
            "diff.indentHeuristic=false",
            "-c",
            "diff.renames=false",
            "-c",
            f"diff.orderFile={os.devnull}",
            "-c",
            "diff.interHunkContext=0",
            "-c",
            "diff.mnemonicPrefix=false",
            "-c",
            "diff.noprefix=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--full-index",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--unified=3",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--no-relative",
            f"-O{os.devnull}",
            "--inter-hunk-context=0",
            "--ignore-submodules=all",
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
        try:
            self._schedule_incremental_removal(path, advance=True)
        except OSError as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
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

    def cleanup_expired(
        self,
        *,
        max_workspaces: int | None = None,
        max_snapshots_per_workspace: int | None = None,
    ) -> None:
        root = self.config.workspace_root
        try:
            root_metadata = root.lstat()
            root_is_directory = stat.S_ISDIR(root_metadata.st_mode)
        except OSError:
            root_is_directory = False
        if not root_is_directory:
            self._hierarchical_reaper.drop_under(root)
            self._drop_reaper_cursors_under(root)
            self._close_incremental_deletions_under(root)
            return
        if self._hierarchical_reaper.exhausted:
            self._hierarchical_reaper.restart()
        workspace_budget = (
            _REAPER_DEFAULT_WORKSPACE_BUDGET if max_workspaces is None else max(0, max_workspaces)
        )
        child_slice = (
            _REAPER_DEFAULT_CHILD_BUDGET
            if max_snapshots_per_workspace is None
            else max(0, max_snapshots_per_workspace)
        )
        if workspace_budget <= 0:
            return
        deadline = time.monotonic() + min(
            _REAPER_MAX_TIME_BUDGET_SECONDS,
            _REAPER_TIME_BUDGET_SECONDS * max(1, workspace_budget),
        )
        admin_deadline = min(
            deadline,
            time.monotonic() + _ADMIN_REAPER_TIME_SLICE_SECONDS,
        )
        self._advance_admin_deletion(
            max_entries=max(1, child_slice),
            absolute_deadline=admin_deadline,
        )
        deletion = self._incremental_reaper_deletion
        if deletion is not None and time.monotonic() < deadline:
            try:
                deletion.step(max_entries=max(1, child_slice), absolute_deadline=deadline)
            except OSError:
                deletion.close()
            if deletion.done:
                deletion.close()
                self._incremental_reaper_deletion = None
        processed_workspaces = 0
        scanned_root_entries = 0
        root_entry_budget = workspace_budget + 1
        while (
            processed_workspaces < workspace_budget
            and scanned_root_entries < root_entry_budget
            and time.monotonic() < deadline
        ):
            entry = self._hierarchical_reaper.next_entry()
            if entry is None:
                self._hierarchical_reaper.restart()
                break
            scanned_root_entries += 1
            if entry.kind == "tombstone" or entry.path.name.startswith(_REAPER_TOMBSTONE_PREFIX):
                self._schedule_incremental_removal(entry.path, advance=False)
                processed_workspaces += 1
                continue
            if entry.kind == "root":
                continue
            if entry.kind != "workspace_start":
                self._hierarchical_reaper.skip_current_workspace()
                continue
            management_root = entry.workspace
            workspace_started_at = time.monotonic()
            remaining_workspace_slots = max(
                1,
                workspace_budget - processed_workspaces,
            )
            workspace_deadline = min(
                deadline,
                workspace_started_at
                + max(0.0, deadline - workspace_started_at)
                / remaining_workspace_slots,
            )
            try:
                self._cleanup_workspace_round(
                    management_root,
                    max_child_entries=child_slice,
                    absolute_deadline=workspace_deadline,
                )
            finally:
                if self._hierarchical_reaper.current_workspace is not None:
                    self._hierarchical_reaper.skip_current_workspace()
            processed_workspaces += 1

    def _reap_analysis_snapshots_locked(
        self,
        management_root: Path,
        *,
        max_snapshots: int | None = None,
    ) -> None:
        snapshots_root = management_root / _ANALYSIS_SNAPSHOTS_DIR
        remaining = max_snapshots
        if snapshots_root.is_dir():
            enumerated = 0
            for entry in self._iter_reaper_entries(
                snapshots_root,
                limit=remaining,
                cursor_key=f"snapshots:{snapshots_root}",
            ):
                enumerated += 1
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                snapshot_root = Path(entry.path)
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
            if remaining is not None:
                remaining = max(0, remaining - enumerated)
        for entry in self._iter_reaper_entries(
            management_root,
            limit=remaining,
            cursor_key=f"indexes:{management_root}",
        ):
            if not entry.name.startswith(".analysis-index-"):
                continue
            index_path = Path(entry.path)
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

    def _iter_reaper_entries(
        self,
        path: Path,
        *,
        limit: int | None,
        cursor_key: str,
    ) -> Iterator[os.DirEntry[str]]:
        if limit is not None and limit <= 0:
            return
        if limit is None:
            self._drop_reaper_cursor(cursor_key)
            try:
                entries = os.scandir(path)
            except OSError:
                return
            with entries:
                yield from entries
            return
        for _ in range(limit):
            entry = self._next_reaper_entry(path, cursor_key)
            if entry is None:
                break
            yield entry

    def _next_reaper_entry(
        self,
        path: Path,
        cursor_key: str,
    ) -> os.DirEntry[str] | None:
        with self._reaper_cursor_lock:
            try:
                details = path.stat(follow_symlinks=False)
            except OSError:
                self._drop_reaper_cursor_locked(cursor_key)
                return None
            if not stat.S_ISDIR(details.st_mode):
                self._drop_reaper_cursor_locked(cursor_key)
                return None
            cursor = self._reaper_cursors.get(cursor_key)
            if (
                cursor is None
                or cursor.path != path
                or cursor.device != details.st_dev
                or cursor.inode != details.st_ino
            ):
                self._drop_reaper_cursor_locked(cursor_key)
                try:
                    iterator = os.scandir(path)
                except OSError:
                    return None
                while len(self._reaper_cursors) >= _REAPER_CURSOR_LIMIT:
                    _, stale = self._reaper_cursors.popitem(last=False)
                    stale.close()
                cursor = _ReaperCursor(
                    path=path,
                    device=details.st_dev,
                    inode=details.st_ino,
                    iterator=iterator,
                )
                self._reaper_cursors[cursor_key] = cursor
            else:
                self._reaper_cursors.move_to_end(cursor_key)
            try:
                return next(cursor.iterator)
            except (StopIteration, OSError):
                self._drop_reaper_cursor_locked(cursor_key)
                return None

    def _drop_reaper_cursor(self, cursor_key: str) -> None:
        with self._reaper_cursor_lock:
            self._drop_reaper_cursor_locked(cursor_key)

    def _drop_reaper_cursor_locked(self, cursor_key: str) -> None:
        cursor = self._reaper_cursors.pop(cursor_key, None)
        if cursor is not None:
            cursor.close()

    def _drop_reaper_cursors_under(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        with self._reaper_cursor_lock:
            keys = [
                key
                for key, cursor in self._reaper_cursors.items()
                if cursor.path.resolve(strict=False) == resolved
                or cursor.path.resolve(strict=False).is_relative_to(resolved)
            ]
            cursors = [self._reaper_cursors.pop(key) for key in keys]
        for cursor in cursors:
            cursor.close()
        self._hierarchical_reaper.drop_under(resolved)
    def _close_all_reaper_cursors(self) -> None:
        with self._reaper_cursor_lock:
            cursors = tuple(self._reaper_cursors.values())
            self._reaper_cursors.clear()
        for cursor in cursors:
            cursor.close()
        self._hierarchical_reaper.close()
        deletion = self._incremental_reaper_deletion
        self._incremental_reaper_deletion = None
        if deletion is not None:
            deletion.close()
        admin_deletion = self._incremental_admin_deletion
        self._incremental_admin_deletion = None
        if admin_deletion is not None:
            admin_deletion.close()
    def start_snapshot_reaper(
        self,
        *,
        interval_seconds: float = 60.0,
        max_workspaces: int = 32,
        max_snapshots_per_workspace: int = 64,
    ) -> None:
        if interval_seconds <= 0 or max_workspaces <= 0 or max_snapshots_per_workspace <= 0:
            raise ValueError("coding analysis reaper bounds must be positive")
        if self._snapshot_reaper_task is not None and not self._snapshot_reaper_task.done():
            return
        self._snapshot_reaper_task = asyncio.create_task(
            self._run_snapshot_reaper(
                interval_seconds=interval_seconds,
                max_workspaces=max_workspaces,
                max_snapshots_per_workspace=max_snapshots_per_workspace,
            ),
            name="assistant-agent-coding-snapshot-reaper",
        )

    def _run_git_stream_input(
        self,
        repo: Path,
        *args: str,
        error_code: str,
        extra_env: Mapping[str, str] | None,
        input_file: BinaryIO,
    ) -> bytes:
        try:
            return _run_git_bytes_process(
                ("git", "-C", str(repo), *args),
                cwd=repo,
                env=_governed_git_environment(extra_env),
                timeout_seconds=20.0,
                max_output_bytes=262_144,
                input_file=input_file,
            )
        except _GitOutputLimitExceeded as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded") from exc
        except (OSError, subprocess.TimeoutExpired, _GitProcessFailed) as exc:
            raise CodingWorkspaceError(error_code) from exc


    def _open_reaper_workspace_lock(self, management_root: Path):
        try:
            lock_path = management_root / _LOCK_FILE
            lock_path.touch(mode=0o600, exist_ok=True)
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
            return handle
        except OSError:
            return None


    def _schedule_incremental_removal(self, path: Path, *, advance: bool) -> Path | None:
        if not path.exists():
            return None
        if path.name.startswith(_REAPER_TOMBSTONE_PREFIX):
            deletion = _IncrementalTombstoneDeletion.from_existing(path)
        else:
            deletion = _IncrementalTombstoneDeletion.rename(path)
        self._drop_reaper_cursors_under(path)
        if self._incremental_reaper_deletion is None:
            self._incremental_reaper_deletion = deletion
            if advance:
                deletion.step(
                    max_entries=_REAPER_DEFAULT_CHILD_BUDGET,
                    absolute_deadline=time.monotonic() + _REAPER_TIME_BUDGET_SECONDS,
                )
                if deletion.done:
                    deletion.close()
                    self._incremental_reaper_deletion = None
        else:
            deletion.close()
        return deletion.tombstone_root



    def _process_hierarchical_reaper_entry(self, entry: _HierarchicalReaperEntry) -> None:
        path = entry.path
        if entry.kind == "root":
            return
        if entry.kind == "tombstone" or path.name.startswith(_REAPER_TOMBSTONE_PREFIX):
            self._schedule_incremental_removal(path, advance=False)
            return
        if entry.kind == "workspace_missing":
            self._drop_reaper_cursors_under(entry.workspace)
            return
        if entry.kind == "workspace_start":
            metadata_path = entry.workspace / _METADATA_FILE
            if not metadata_path.is_file():
                self._hierarchical_reaper.skip_current_workspace()
                return
            try:
                self._load_metadata(metadata_path)
            except CodingWorkspaceError:
                self._hierarchical_reaper.skip_current_workspace()
            return
        handle = self._open_reaper_workspace_lock(entry.workspace)
        if handle is None:
            if entry.kind in {"snapshot", "management"}:
                self._hierarchical_reaper.skip_current_workspace()
            return
        try:
            if entry.kind == "snapshot":
                self._reap_snapshot_path_bounded(path)
            elif entry.kind == "management":
                if path.name.startswith(".analysis-index-") and path.is_file():
                    path.unlink(missing_ok=True)
            elif entry.kind == "workspace":
                self._reap_workspace_path_bounded(entry.workspace)
        finally:
            handle.close()


    def _reap_snapshot_path_bounded(self, snapshot_root: Path) -> None:
        try:
            if not snapshot_root.is_dir():
                return
            if snapshot_root.name.startswith(_ANALYSIS_BUILD_PREFIX):
                self._schedule_incremental_removal(snapshot_root, advance=False)
                return
            if snapshot_root.name.startswith(_ANALYSIS_QUARANTINE_PREFIX):
                quarantined_at = datetime.fromtimestamp(
                    snapshot_root.stat(follow_symlinks=False).st_mtime,
                    timezone.utc,
                )
                if quarantined_at + timedelta(seconds=_ANALYSIS_QUARANTINE_SECONDS) <= self._clock():
                    self._schedule_incremental_removal(snapshot_root, advance=False)
                return
            try:
                metadata = self._load_analysis_snapshot_metadata(
                    snapshot_root / _ANALYSIS_SNAPSHOT_METADATA_FILE
                )
            except CodingWorkspaceError:
                self._quarantine_analysis_snapshot(snapshot_root)
                return
            if metadata.snapshot.expires_at <= self._clock():
                self._schedule_incremental_removal(snapshot_root, advance=False)
        except OSError:
            return


    def _reap_workspace_path_bounded(self, management_root: Path) -> None:
        try:
            metadata = self._load_metadata(management_root / _METADATA_FILE)
        except CodingWorkspaceError:
            return
        if metadata.expires_at > self._clock():
            return
        self._retire_expired_workspace(
            management_root,
            metadata,
            absolute_deadline=time.monotonic() + _REAPER_TIME_BUDGET_SECONDS,
        )

    def _load_cleanup_progress(self, management_root: Path) -> tuple[str, int]:
        progress_path = management_root / _REAPER_PROGRESS_FILE
        try:
            descriptor = os.open(progress_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return "snapshot", 0
        try:
            payload = os.read(descriptor, 1_025)
        finally:
            os.close(descriptor)
        if len(payload) > 1_024:
            return "snapshot", 0
        try:
            decoded = json.loads(payload.decode("utf-8"))
            phase = decoded["phase"]
            cookie = decoded["cookie"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            return "snapshot", 0
        if phase not in {"snapshot", "management"}:
            return "snapshot", 0
        if not isinstance(cookie, int) or isinstance(cookie, bool) or not 0 <= cookie <= (2**63 - 1):
            return "snapshot", 0
        return phase, cookie


    def _write_cleanup_progress(self, management_root: Path, *, phase: str, cookie: int) -> None:
        if phase not in {"snapshot", "management"} or not 0 <= cookie <= (2**63 - 1):
            raise CodingWorkspaceError("workspace_cleanup_failed")
        progress_path = management_root / _REAPER_PROGRESS_FILE
        temporary = management_root / f".{_REAPER_PROGRESS_FILE}.{os.getpid()}.{time.monotonic_ns()}"
        payload = json.dumps(
            {"schema_version": 1, "phase": phase, "cookie": cookie},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, progress_path)
        finally:
            temporary.unlink(missing_ok=True)


    def _cleanup_workspace_child_slice(
        self,
        management_root: Path,
        *,
        max_entries: int,
        absolute_deadline: float,
    ) -> None:
        if max_entries <= 0:
            return
        phase, cookie = self._load_cleanup_progress(management_root)
        remaining = max_entries
        while remaining > 0 and time.monotonic() < absolute_deadline:
            directory = (
                management_root / _ANALYSIS_SNAPSHOTS_DIR if phase == "snapshot" else management_root
            )
            page = _read_persistent_directory_page(
                directory,
                cookie=cookie,
                max_entries=remaining,
                absolute_deadline=absolute_deadline,
            )
            remaining -= page.consumed
            for entry in page.entries:
                if time.monotonic() >= absolute_deadline:
                    break
                if phase == "snapshot":
                    self._reap_snapshot_path_bounded(entry.path)
                elif entry.name.startswith(".analysis-index-"):
                    try:
                        if entry.path.is_file():
                            entry.path.unlink(missing_ok=True)
                    except OSError:
                        pass
            cookie = page.cookie
            if page.done:
                if phase == "snapshot":
                    phase, cookie = "management", 0
                else:
                    phase, cookie = "snapshot", 0
                    self._write_cleanup_progress(management_root, phase=phase, cookie=cookie)
                    break
            self._write_cleanup_progress(management_root, phase=phase, cookie=cookie)
            if page.consumed == 0 and not page.done:
                break


    def _cleanup_workspace_round(
        self,
        management_root: Path,
        *,
        max_child_entries: int,
        absolute_deadline: float,
    ) -> None:
        metadata_path = management_root / _METADATA_FILE
        if not metadata_path.is_file():
            return
        handle = self._open_reaper_workspace_lock(management_root)
        if handle is None:
            return
        try:
            try:
                metadata = self._load_metadata(metadata_path)
            except CodingWorkspaceError:
                return
            if metadata.expires_at <= self._clock():
                self._retire_expired_workspace(
                    management_root,
                    metadata,
                    absolute_deadline=absolute_deadline,
                )
                return
            self._cleanup_workspace_child_slice(
                management_root,
                max_entries=max_child_entries,
                absolute_deadline=absolute_deadline,
            )
        finally:
            handle.close()


    def _read_small_regular_file_at(
        self,
        parent_fd: int,
        name: str,
        *,
        max_bytes: int = 4_096,
    ) -> str | None:
        if not name or name in {".", ".."} or "/" in name:
            return None
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                return None
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    return None
                payload = bytearray()
                while len(payload) <= max_bytes:
                    chunk = os.read(descriptor, min(4_096, max_bytes + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
            finally:
                os.close(descriptor)
        except OSError:
            return None
        if len(payload) > max_bytes:
            return None
        try:
            return payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None


    def _open_absolute_directory_no_follow(self, path: Path) -> tuple[Path, int] | None:
        absolute = Path(os.path.abspath(os.fspath(path)))
        if not absolute.is_absolute():
            return None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(absolute.anchor, flags)
            for component in absolute.parts[1:]:
                if component in {"", ".", ".."}:
                    raise OSError("unsafe directory component")
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    details = os.fstat(child)
                    if not stat.S_ISDIR(details.st_mode):
                        raise OSError("not a directory")
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return absolute, descriptor
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            return None


    def _configured_repository_common_dir(
        self,
        repository,
        *,
        absolute_deadline: float,
    ) -> tuple[Path, int] | None:
        remaining = absolute_deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            payload = _run_git_bytes_process(
                (
                    "git",
                    "-C",
                    str(repository.path),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ),
                cwd=repository.path,
                env=_governed_git_environment(),
                timeout_seconds=remaining,
                max_output_bytes=4_096,
            )
            decoded = payload.decode("utf-8").strip()
        except (
            OSError,
            UnicodeDecodeError,
            subprocess.TimeoutExpired,
            _GitOutputLimitExceeded,
            _GitProcessFailed,
        ):
            return None
        if not decoded or "\0" in decoded or "\n" in decoded or "\r" in decoded:
            return None
        common_path = Path(decoded)
        if not common_path.is_absolute():
            common_path = repository.path / common_path
        return self._open_absolute_directory_no_follow(common_path)


    def _validate_admin_directory_tree(
        self,
        admin_fd: int,
        *,
        absolute_deadline: float,
    ) -> bool:
        visited = 0
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

        def validate(directory_fd: int, depth: int) -> bool:
            nonlocal visited
            if depth > _ADMIN_VALIDATION_DEPTH_LIMIT:
                return False
            try:
                with os.scandir(directory_fd) as iterator:
                    for entry in iterator:
                        if time.monotonic() >= absolute_deadline:
                            return False
                        visited += 1
                        if visited > _ADMIN_VALIDATION_ENTRY_LIMIT:
                            return False
                        name = entry.name
                        if not name or name in {".", ".."} or "/" in name:
                            return False
                        details = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        if stat.S_ISREG(details.st_mode):
                            descriptor = os.open(
                                name,
                                os.O_RDONLY
                                | getattr(os, "O_NOFOLLOW", 0)
                                | getattr(os, "O_CLOEXEC", 0),
                                dir_fd=directory_fd,
                            )
                            try:
                                opened = os.fstat(descriptor)
                                if (
                                    not stat.S_ISREG(opened.st_mode)
                                    or opened.st_dev != details.st_dev
                                    or opened.st_ino != details.st_ino
                                ):
                                    return False
                            finally:
                                os.close(descriptor)
                            continue
                        if not stat.S_ISDIR(details.st_mode):
                            return False
                        if depth == 0 and name not in {"logs", "refs"}:
                            return False
                        child_fd = os.open(
                            name,
                            directory_flags,
                            dir_fd=directory_fd,
                        )
                        try:
                            opened = os.fstat(child_fd)
                            if (
                                not stat.S_ISDIR(opened.st_mode)
                                or opened.st_dev != details.st_dev
                                or opened.st_ino != details.st_ino
                                or not validate(child_fd, depth + 1)
                            ):
                                return False
                        finally:
                            os.close(child_fd)
            except OSError:
                return False
            return True

        return validate(admin_fd, 0)


    def _validated_worktree_admin(
        self,
        repo_root: Path,
        repository,
        *,
        absolute_deadline: float,
    ) -> _ValidatedWorktreeAdmin | None:
        configured_common = self._configured_repository_common_dir(
            repository,
            absolute_deadline=absolute_deadline,
        )
        if configured_common is None:
            return None
        common_root, common_fd = configured_common
        repo_opened = self._open_absolute_directory_no_follow(repo_root)
        if repo_opened is None:
            os.close(common_fd)
            return None
        normalized_repo_root, repo_fd = repo_opened
        worktrees_fd = -1
        admin_fd = -1
        try:
            raw_gitdir = self._read_small_regular_file_at(repo_fd, ".git")
            if raw_gitdir is None or not raw_gitdir.startswith("gitdir:"):
                return None
            raw_admin_path = raw_gitdir.removeprefix("gitdir:").strip()
            if not raw_admin_path or "\n" in raw_admin_path or "\r" in raw_admin_path:
                return None
            admin_path = Path(raw_admin_path)
            if not admin_path.is_absolute():
                admin_path = normalized_repo_root / admin_path
            admin_path = Path(os.path.abspath(os.fspath(admin_path)))
            expected_worktrees = common_root / "worktrees"
            if admin_path.parent != expected_worktrees:
                return None
            admin_name = admin_path.name
            if not admin_name or admin_name in {".", ".."} or "/" in admin_name:
                return None

            worktrees_metadata = os.stat(
                "worktrees",
                dir_fd=common_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(worktrees_metadata.st_mode):
                return None
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            worktrees_fd = os.open("worktrees", directory_flags, dir_fd=common_fd)
            opened_worktrees = os.fstat(worktrees_fd)
            if (
                opened_worktrees.st_dev != worktrees_metadata.st_dev
                or opened_worktrees.st_ino != worktrees_metadata.st_ino
            ):
                return None
            admin_metadata = os.stat(
                admin_name,
                dir_fd=worktrees_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(admin_metadata.st_mode):
                return None
            admin_fd = os.open(admin_name, directory_flags, dir_fd=worktrees_fd)
            opened_admin = os.fstat(admin_fd)
            if (
                opened_admin.st_dev != admin_metadata.st_dev
                or opened_admin.st_ino != admin_metadata.st_ino
            ):
                return None

            raw_common = self._read_small_regular_file_at(admin_fd, "commondir")
            raw_backref = self._read_small_regular_file_at(admin_fd, "gitdir")
            if raw_common is None or raw_backref is None:
                return None
            if any(character in raw_common for character in ("\0", "\n", "\r")):
                return None
            if any(character in raw_backref for character in ("\0", "\n", "\r")):
                return None
            declared_common = Path(raw_common)
            if not declared_common.is_absolute():
                declared_common = admin_path / declared_common
            declared_common = Path(os.path.abspath(os.fspath(declared_common)))
            declared_backref = Path(raw_backref)
            if not declared_backref.is_absolute():
                declared_backref = admin_path / declared_backref
            declared_backref = Path(os.path.abspath(os.fspath(declared_backref)))
            if declared_common != common_root:
                return None
            if declared_backref != normalized_repo_root / ".git":
                return None
            if not self._validate_admin_directory_tree(
                admin_fd,
                absolute_deadline=absolute_deadline,
            ):
                return None
            result = _ValidatedWorktreeAdmin(
                common_root=common_root,
                common_fd=common_fd,
                worktrees_fd=worktrees_fd,
                admin_fd=admin_fd,
                admin_name=admin_name,
            )
            common_fd = -1
            worktrees_fd = -1
            admin_fd = -1
            return result
        except OSError:
            return None
        finally:
            os.close(repo_fd)
            for descriptor in (admin_fd, worktrees_fd, common_fd):
                if descriptor >= 0:
                    os.close(descriptor)


    def _open_admin_tombstone_area(self, binding: _ValidatedWorktreeAdmin) -> int | None:
        try:
            try:
                os.mkdir(_ADMIN_TOMBSTONE_AREA, 0o700, dir_fd=binding.common_fd)
            except FileExistsError:
                pass
            metadata = os.stat(
                _ADMIN_TOMBSTONE_AREA,
                dir_fd=binding.common_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                return None
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            area_fd = os.open(_ADMIN_TOMBSTONE_AREA, flags, dir_fd=binding.common_fd)
            opened = os.fstat(area_fd)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_dev != os.fstat(binding.worktrees_fd).st_dev
            ):
                os.close(area_fd)
                return None
            os.fchmod(area_fd, 0o700)
            return area_fd
        except OSError:
            return None


    def _choose_admin_tombstone_name(self, area_fd: int, admin_name: str) -> str | None:
        for _attempt in range(8):
            candidate = (
                f"{_ADMIN_TOMBSTONE_PREFIX}{admin_name}-{os.getpid()}-"
                f"{time.monotonic_ns()}-{secrets.token_hex(16)}"
            )
            try:
                os.stat(candidate, dir_fd=area_fd, follow_symlinks=False)
            except FileNotFoundError:
                return candidate
            except OSError:
                return None
        return None


    def _register_workspace_deletion(
        self,
        deletion: _IncrementalTombstoneDeletion,
    ) -> None:
        if self._incremental_reaper_deletion is None:
            self._incremental_reaper_deletion = deletion
        else:
            deletion.close()


    def _register_admin_deletion(self, tombstone: Path) -> None:
        deletion = _IncrementalTombstoneDeletion.from_existing(tombstone)
        if self._incremental_admin_deletion is None:
            self._incremental_admin_deletion = deletion
        else:
            deletion.close()


    def _admin_cleanup_pending(self) -> bool:
        marker = self.config.workspace_root / _ADMIN_REAPER_PENDING_FILE
        try:
            return stat.S_ISREG(marker.lstat().st_mode)
        except OSError:
            return False


    def _mark_admin_cleanup_pending(self) -> bool:
        marker = self.config.workspace_root / _ADMIN_REAPER_PENDING_FILE
        try:
            descriptor = os.open(
                marker,
                os.O_WRONLY
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    return False
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            return True
        except OSError:
            return False


    def _clear_admin_cleanup_pending(self) -> None:
        marker = self.config.workspace_root / _ADMIN_REAPER_PENDING_FILE
        try:
            if stat.S_ISREG(marker.lstat().st_mode):
                marker.unlink()
        except OSError:
            pass


    def _discover_admin_tombstone(
        self,
        *,
        absolute_deadline: float,
    ) -> tuple[_IncrementalTombstoneDeletion | None, bool]:
        repositories = tuple(self.config.repositories.values())
        if not repositories or time.monotonic() >= absolute_deadline:
            return None, False
        index = self._admin_repository_cursor % len(repositories)
        self._admin_repository_cursor = (index + 1) % len(repositories)
        configured_common = self._configured_repository_common_dir(
            repositories[index],
            absolute_deadline=absolute_deadline,
        )
        if configured_common is None:
            return None, False
        common_root, common_fd = configured_common
        area_fd = -1
        try:
            metadata = os.stat(
                _ADMIN_TOMBSTONE_AREA,
                dir_fd=common_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                return None, False
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            area_fd = os.open(_ADMIN_TOMBSTONE_AREA, flags, dir_fd=common_fd)
            opened = os.fstat(area_fd)
            if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                return None, False
            with os.scandir(area_fd) as iterator:
                for enumerated, entry in enumerate(iterator, start=1):
                    if enumerated > _ADMIN_DISCOVERY_ENTRY_BUDGET:
                        return None, False
                    if time.monotonic() >= absolute_deadline:
                        return None, False
                    if not entry.name.startswith(_ADMIN_TOMBSTONE_PREFIX):
                        continue
                    details = os.stat(
                        entry.name,
                        dir_fd=area_fd,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISDIR(details.st_mode):
                        continue
                    child_fd = os.open(entry.name, flags, dir_fd=area_fd)
                    try:
                        opened_child = os.fstat(child_fd)
                        if (
                            opened_child.st_dev != details.st_dev
                            or opened_child.st_ino != details.st_ino
                        ):
                            continue
                    finally:
                        os.close(child_fd)
                    return (
                        _IncrementalTombstoneDeletion.from_existing(
                            common_root / _ADMIN_TOMBSTONE_AREA / entry.name
                        ),
                        True,
                    )
            return None, True
        except FileNotFoundError:
            return None, True
        except (NotADirectoryError, OSError):
            return None, False
        finally:
            if area_fd >= 0:
                os.close(area_fd)
            os.close(common_fd)
        return None, True


    def _advance_admin_deletion(
        self,
        *,
        max_entries: int,
        absolute_deadline: float,
    ) -> None:
        deletion = self._incremental_admin_deletion
        if deletion is None:
            if not self._admin_cleanup_pending():
                self._admin_empty_repository_scans = 0
                return
            deletion, scan_complete = self._discover_admin_tombstone(
                absolute_deadline=absolute_deadline,
            )
            if deletion is None:
                if scan_complete:
                    self._admin_empty_repository_scans += 1
                    if self._admin_empty_repository_scans >= max(
                        1,
                        len(self.config.repositories),
                    ):
                        self._clear_admin_cleanup_pending()
                        self._admin_empty_repository_scans = 0
                return
            self._admin_empty_repository_scans = 0
            self._incremental_admin_deletion = deletion
        if time.monotonic() >= absolute_deadline:
            deletion.close()
            return
        try:
            deletion.step(
                max_entries=max(1, max_entries),
                absolute_deadline=absolute_deadline,
            )
        except OSError:
            deletion.close()
            return
        if deletion.done:
            deletion.close()
            self._incremental_admin_deletion = None


    def _close_incremental_deletions_under(self, root: Path) -> None:
        normalized_root = Path(os.path.abspath(os.fspath(root)))
        workspace_deletion = self._incremental_reaper_deletion
        if workspace_deletion is not None:
            workspace_deletion.close()
            normalized = Path(os.path.abspath(os.fspath(workspace_deletion.tombstone_root)))
            if normalized == normalized_root or normalized.is_relative_to(normalized_root):
                self._incremental_reaper_deletion = None
        admin_deletion = self._incremental_admin_deletion
        if admin_deletion is not None:
            admin_deletion.close()
            normalized = Path(os.path.abspath(os.fspath(admin_deletion.tombstone_root)))
            if normalized == normalized_root or normalized.is_relative_to(normalized_root):
                self._incremental_admin_deletion = None


    def _retire_expired_workspace(
        self,
        management_root: Path,
        metadata,
        *,
        absolute_deadline: float,
    ) -> None:
        repository = self.config.repositories.get(metadata.repo_id)
        if repository is None or time.monotonic() >= absolute_deadline:
            return
        binding = self._validated_worktree_admin(
            management_root / _REPO_DIR,
            repository,
            absolute_deadline=absolute_deadline,
        )
        if binding is None:
            return
        area_fd = -1
        workspace_deletion: _IncrementalTombstoneDeletion | None = None
        try:
            area_fd = self._open_admin_tombstone_area(binding)
            if area_fd is None or time.monotonic() >= absolute_deadline:
                return
            admin_tombstone_name = self._choose_admin_tombstone_name(
                area_fd,
                binding.admin_name,
            )
            if admin_tombstone_name is None or time.monotonic() >= absolute_deadline:
                return
            if not self._mark_admin_cleanup_pending():
                return
            workspace_deletion = _IncrementalTombstoneDeletion.rename(management_root)
            try:
                if time.monotonic() >= absolute_deadline:
                    raise TimeoutError
                os.rename(
                    binding.admin_name,
                    admin_tombstone_name,
                    src_dir_fd=binding.worktrees_fd,
                    dst_dir_fd=area_fd,
                )
            except (OSError, TimeoutError):
                workspace_deletion.close()
                try:
                    os.rename(workspace_deletion.tombstone_root, management_root)
                except OSError:
                    pass
                return
            self._drop_reaper_cursors_under(management_root)
            self._register_workspace_deletion(workspace_deletion)
            workspace_deletion = None
            self._register_admin_deletion(
                binding.common_root / _ADMIN_TOMBSTONE_AREA / admin_tombstone_name
            )
        finally:
            if workspace_deletion is not None:
                workspace_deletion.close()
            if area_fd is not None and area_fd >= 0:
                os.close(area_fd)
            binding.close()


    async def _run_snapshot_reaper(
        self,
        *,
        interval_seconds: float,
        max_workspaces: int,
        max_snapshots_per_workspace: int,
    ) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(
                    self.cleanup_expired,
                    max_workspaces=max_workspaces,
                    max_snapshots_per_workspace=max_snapshots_per_workspace,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def aclose(self) -> None:
        task = self._snapshot_reaper_task
        self._snapshot_reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.to_thread(
                self.cleanup_expired,
                max_workspaces=32,
                max_snapshots_per_workspace=64,
            )
        finally:
            self._close_all_reaper_cursors()

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
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
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
        env = _governed_git_environment(extra_env)
        input_file = None
        try:
            if input_bytes is not None:
                input_file = tempfile.TemporaryFile()
                input_file.write(input_bytes)
                input_file.seek(0)
            return _run_git_bytes_process(
                ("git", "-C", str(repo), *args),
                cwd=repo,
                env=env,
                timeout_seconds=20.0,
                max_output_bytes=max_output_bytes if max_output_bytes is not None else 262_144,
                input_file=input_file,
            )
        except _GitOutputLimitExceeded as exc:
            raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded") from exc
        except (OSError, subprocess.TimeoutExpired, _GitProcessFailed) as exc:
            raise CodingWorkspaceError(error_code) from exc
        finally:
            if input_file is not None:
                input_file.close()

def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _GitOutputLimitExceeded(RuntimeError):
    pass


class _GitProcessFailed(RuntimeError):
    def __init__(self, returncode: int, stderr: bytes) -> None:
        super().__init__(f"git exited with status {returncode}")
        self.returncode = returncode
        self.stderr = stderr


def _governed_git_environment(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }
    if extra_env:
        env.update(extra_env)
    return env




def _terminate_git_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        finally:
            try:
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass


def _iter_process_pipe_events(
    process: subprocess.Popen[bytes],
    *,
    command: Sequence[str],
    absolute_deadline: float,
    chunk_bytes: int = 65_536,
) -> Iterator[tuple[str, bytes]]:
    selector = selectors.DefaultSelector()
    try:
        for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                raise OSError(f"git {stream_name} pipe was not created")
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, stream_name)
        while selector.get_map():
            remaining = absolute_deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(tuple(command), 0)
            events = selector.select(timeout=remaining)
            if not events:
                if time.monotonic() >= absolute_deadline:
                    raise subprocess.TimeoutExpired(tuple(command), 0)
                continue
            for key, _mask in events:
                while True:
                    if time.monotonic() >= absolute_deadline:
                        raise subprocess.TimeoutExpired(tuple(command), 0)
                    try:
                        chunk = os.read(key.fd, chunk_bytes)
                    except BlockingIOError:
                        break
                    if not chunk:
                        selector.unregister(key.fd)
                        break
                    yield str(key.data), chunk
                    if len(chunk) < chunk_bytes:
                        break
    finally:
        selector.close()

def _run_git_bytes_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
    input_file: BinaryIO | None = None,
) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    events = None
    chunks: list[bytes] = []
    stderr = bytearray()
    total = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            tuple(command),
            cwd=cwd,
            env=dict(env),
            stdin=input_file if input_file is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        events = _iter_process_pipe_events(
            process,
            command=command,
            absolute_deadline=deadline,
        )
        for stream_name, chunk in events:
            if stream_name == "stderr":
                remaining_stderr = 8_192 - len(stderr)
                if remaining_stderr > 0:
                    stderr.extend(chunk[:remaining_stderr])
                continue
            total += len(chunk)
            if total > max_output_bytes:
                raise _GitOutputLimitExceeded
            chunks.append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(tuple(command), timeout_seconds)
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            raise _GitProcessFailed(returncode, bytes(stderr))
        return b"".join(chunks)
    finally:
        if events is not None:
            events.close()
        if process is not None:
            _terminate_git_process(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()



def _iter_git_nul_records_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_record_bytes: int,
    consume_record: Callable[[int], None],
    consume_bytes: Callable[[int], None] | None = None,
    chunk_bytes: int = 65_536,
) -> Iterator[bytes]:
    process: subprocess.Popen[bytes] | None = None
    events = None
    pending = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            tuple(command),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        events = _iter_process_pipe_events(
            process,
            command=command,
            absolute_deadline=deadline,
            chunk_bytes=max(1, min(chunk_bytes, 65_536)),
        )
        for stream_name, chunk in events:
            if stream_name == "stderr":
                remaining_stderr = 8_192 - len(stderr)
                if remaining_stderr > 0:
                    stderr.extend(chunk[:remaining_stderr])
                continue
            if consume_bytes is not None:
                consume_bytes(len(chunk))
            offset = 0
            while offset < len(chunk):
                terminator = chunk.find(b"\0", offset)
                if terminator < 0:
                    pending.extend(chunk[offset:])
                    if len(pending) > max_record_bytes:
                        raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded")
                    break
                pending.extend(chunk[offset:terminator])
                if len(pending) > max_record_bytes:
                    raise CodingWorkspaceError("coding_analysis_snapshot_limit_exceeded")
                consume_record(len(pending) + 1)
                yield bytes(pending)
                pending.clear()
                offset = terminator + 1
        if pending:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(tuple(command), timeout_seconds)
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            raise _GitProcessFailed(returncode, bytes(stderr))
    finally:
        if events is not None:
            events.close()
        if process is not None:
            _terminate_git_process(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()



_GETDENTS_LIBC = ctypes.CDLL(None, use_errno=True)
_GETDENTS64 = getattr(_GETDENTS_LIBC, "getdents64", None)
if _GETDENTS64 is not None:
    _GETDENTS64.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t)
    _GETDENTS64.restype = ctypes.c_ssize_t


@dataclass(frozen=True)
class _PersistentDirectoryEntry:
    path: Path
    name: str
    directory_type: int

    def is_dir(self, *, follow_symlinks: bool = False) -> bool:
        if self.directory_type == 4:
            return True
        if self.directory_type == 10 and not follow_symlinks:
            return False
        try:
            return stat.S_ISDIR(self.path.stat(follow_symlinks=follow_symlinks).st_mode)
        except OSError:
            return False


@dataclass(frozen=True)
class _PersistentDirectoryPage:
    entries: tuple[_PersistentDirectoryEntry, ...]
    cookie: int
    done: bool
    consumed: int


def _read_persistent_directory_page(
    directory: Path,
    *,
    cookie: int,
    max_entries: int,
    absolute_deadline: float,
) -> _PersistentDirectoryPage:
    if max_entries <= 0 or time.monotonic() >= absolute_deadline:
        return _PersistentDirectoryPage((), cookie, False, 0)
    if _GETDENTS64 is None:
        raise OSError("getdents64 is unavailable on this platform")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
    except (FileNotFoundError, NotADirectoryError):
        return _PersistentDirectoryPage((), 0, True, 0)
    entries: list[_PersistentDirectoryEntry] = []
    consumed = 0
    current_cookie = cookie
    done = False
    buffer = ctypes.create_string_buffer(4_096)
    try:
        if cookie:
            try:
                os.lseek(descriptor, cookie, os.SEEK_SET)
            except OSError:
                os.lseek(descriptor, 0, os.SEEK_SET)
                current_cookie = 0
        while consumed < max_entries:
            if time.monotonic() >= absolute_deadline:
                break
            read_size = _GETDENTS64(descriptor, ctypes.byref(buffer), ctypes.sizeof(buffer))
            if read_size < 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number), str(directory))
            if read_size == 0:
                done = True
                break
            payload = buffer.raw[:read_size]
            offset = 0
            while offset < read_size and consumed < max_entries:
                if time.monotonic() >= absolute_deadline:
                    break
                if offset + 19 > read_size:
                    raise OSError("malformed getdents64 record")
                next_cookie = struct.unpack_from("q", payload, offset + 8)[0]
                record_length = struct.unpack_from("H", payload, offset + 16)[0]
                if record_length < 20 or offset + record_length > read_size:
                    raise OSError("malformed getdents64 record")
                directory_type = payload[offset + 18]
                name_start = offset + 19
                name_end = payload.find(b"\0", name_start, offset + record_length)
                if name_end < 0:
                    raise OSError("malformed getdents64 name")
                raw_name = payload[name_start:name_end]
                current_cookie = next_cookie
                consumed += 1
                if raw_name not in {b".", b".."}:
                    name = os.fsdecode(raw_name)
                    entries.append(
                        _PersistentDirectoryEntry(
                            path=directory / name,
                            name=name,
                            directory_type=directory_type,
                        )
                    )
                offset += record_length
    finally:
        os.close(descriptor)
    return _PersistentDirectoryPage(tuple(entries), current_cookie, done, consumed)

def _detect_git_object_format(repository: Path, env: Mapping[str, str]) -> str:
    try:
        output = _run_git_bytes_process(
            ("git", "-C", str(repository), "rev-parse", "--show-object-format"),
            cwd=repository,
            env=env,
            timeout_seconds=20.0,
            max_output_bytes=16,
        )
        object_format = output.decode("ascii").strip()
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired, _GitProcessFailed, _GitOutputLimitExceeded) as exc:
        raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
    if object_format not in {"sha1", "sha256"}:
        raise CodingWorkspaceError("coding_analysis_snapshot_failed")
    return object_format


def _initialize_isolated_git_dir(git_dir: Path, object_format: str) -> None:
    if object_format not in {"sha1", "sha256"}:
        raise CodingWorkspaceError("coding_analysis_snapshot_failed")
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (git_dir / "objects" / "pack").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/analysis\n", encoding="ascii")
    repository_format = "1" if object_format == "sha256" else "0"
    extensions = "[extensions]\n\tobjectFormat = sha256\n" if object_format == "sha256" else ""
    (git_dir / "config").write_text(
        "[core]\n"
        f"\trepositoryformatversion = {repository_format}\n"
        "\tbare = true\n"
        f"{extensions}",
        encoding="ascii",
    )


@dataclass(frozen=True)
class _HierarchicalReaperEntry:
    kind: str
    workspace: Path
    path: Path


class _HierarchicalReaperTraversal:
    def __init__(
        self,
        root: Path,
        *,
        child_directories: Sequence[tuple[str, str]],
    ) -> None:
        self.root = root
        self.child_directories = tuple(child_directories)
        self.current_workspace: Path | None = None
        self._root_iterator: os.ScandirIterator[str] | None = None
        self._child_iterator: os.ScandirIterator[str] | None = None
        self._child_root: Path | None = None
        self._child_phase = 0
        self._workspace_started = False
        self._exhausted = False

    @property
    def active_paths(self) -> tuple[Path, ...]:
        paths = [self.root]
        if self.current_workspace is not None:
            paths.append(self.current_workspace)
        if self._child_root is not None:
            paths.append(self._child_root)
        return tuple(paths)

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def _close_child(self) -> None:
        if self._child_iterator is not None:
            self._child_iterator.close()
        self._child_iterator = None
        self._child_root = None

    def _clear_workspace(self) -> None:
        self._close_child()
        self.current_workspace = None
        self._child_phase = 0
        self._workspace_started = False

    def skip_current_workspace(self) -> None:
        self._clear_workspace()

    def next_entry(self) -> _HierarchicalReaperEntry | None:
        while True:
            if self.current_workspace is None:
                if self._exhausted:
                    return None
                if self._root_iterator is None:
                    try:
                        self._root_iterator = os.scandir(self.root)
                    except (FileNotFoundError, NotADirectoryError):
                        self._exhausted = True
                        return None
                try:
                    root_entry = next(self._root_iterator)
                except StopIteration:
                    self._root_iterator.close()
                    self._root_iterator = None
                    self._exhausted = True
                    return None
                except OSError:
                    self.close()
                    return None
                root_path = Path(root_entry.path)
                try:
                    is_directory = root_entry.is_dir(follow_symlinks=False)
                except OSError:
                    return _HierarchicalReaperEntry("root", root_path, root_path)
                if not is_directory:
                    return _HierarchicalReaperEntry("root", root_path, root_path)
                if root_path.name.startswith(_REAPER_TOMBSTONE_PREFIX):
                    return _HierarchicalReaperEntry("tombstone", root_path, root_path)
                self.current_workspace = root_path
                self._child_phase = 0
                self._workspace_started = False
            workspace = self.current_workspace
            if not self._workspace_started:
                self._workspace_started = True
                return _HierarchicalReaperEntry("workspace_start", workspace, workspace)
            while self._child_phase < len(self.child_directories):
                kind, relative_root = self.child_directories[self._child_phase]
                if self._child_iterator is None:
                    child_root = workspace if relative_root == "." else workspace / relative_root
                    try:
                        self._child_iterator = os.scandir(child_root)
                        self._child_root = child_root
                    except (FileNotFoundError, NotADirectoryError):
                        self._child_phase += 1
                        continue
                try:
                    child_entry = next(self._child_iterator)
                except StopIteration:
                    self._close_child()
                    self._child_phase += 1
                    continue
                except OSError:
                    self._clear_workspace()
                    return _HierarchicalReaperEntry("workspace_missing", workspace, workspace)
                return _HierarchicalReaperEntry(kind, workspace, Path(child_entry.path))
            completed = workspace
            self._clear_workspace()
            return _HierarchicalReaperEntry("workspace", completed, completed)

    def drop_under(self, path: Path) -> None:
        path = path.resolve(strict=False)
        workspace = self.current_workspace
        if workspace is not None:
            resolved_workspace = workspace.resolve(strict=False)
            if resolved_workspace == path or resolved_workspace.is_relative_to(path):
                self._clear_workspace()
            elif self._child_root is not None:
                child_root = self._child_root.resolve(strict=False)
                if child_root == path or child_root.is_relative_to(path):
                    self._close_child()
        resolved_root = self.root.resolve(strict=False)
        if resolved_root == path or resolved_root.is_relative_to(path):
            self.close()

    def restart(self) -> None:
        self.close()
        self._exhausted = False

    def close(self) -> None:
        self._clear_workspace()
        if self._root_iterator is not None:
            self._root_iterator.close()
        self._root_iterator = None
        self._exhausted = True


class _IncrementalTombstoneDeletion:
    def __init__(self, tombstone_root: Path) -> None:
        self.tombstone_root = tombstone_root
        self._current_directory = tombstone_root
        self._iterator: os.ScandirIterator[str] | None = None
        self.done = not tombstone_root.exists()

    @classmethod
    def rename(cls, source: Path) -> _IncrementalTombstoneDeletion:
        for attempt in range(8):
            tombstone = source.with_name(
                f"{_REAPER_TOMBSTONE_PREFIX}{source.name}-{os.getpid()}-{time.monotonic_ns()}-{attempt}"
            )
            try:
                os.replace(source, tombstone)
                return cls(tombstone)
            except FileExistsError:
                continue
        raise CodingWorkspaceError("coding_analysis_snapshot_failed")

    @classmethod
    def from_existing(cls, tombstone: Path) -> _IncrementalTombstoneDeletion:
        return cls(tombstone)

    def _close_iterator(self) -> None:
        if self._iterator is not None:
            self._iterator.close()
        self._iterator = None

    def step(self, *, max_entries: int, absolute_deadline: float | None = None) -> int:
        deadline = float("inf") if absolute_deadline is None else absolute_deadline
        consumed = 0
        while not self.done and consumed < max_entries:
            if time.monotonic() >= deadline:
                break
            if self._iterator is None:
                try:
                    if time.monotonic() >= deadline:
                        break
                    self._current_directory.chmod(0o700)
                    if time.monotonic() >= deadline:
                        break
                    self._iterator = os.scandir(self._current_directory)
                except (FileNotFoundError, NotADirectoryError):
                    if self._current_directory == self.tombstone_root:
                        self.done = True
                    else:
                        self._current_directory = self._current_directory.parent
                    continue
            if time.monotonic() >= deadline:
                break
            try:
                entry = next(self._iterator)
            except StopIteration:
                self._close_iterator()
                current = self._current_directory
                if time.monotonic() >= deadline:
                    break
                try:
                    current.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                consumed += 1
                if current == self.tombstone_root:
                    self.done = True
                else:
                    self._current_directory = current.parent
                continue
            consumed += 1
            entry_path = Path(entry.path)
            if time.monotonic() >= deadline:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    self._close_iterator()
                    self._current_directory = entry_path
                else:
                    if time.monotonic() >= deadline:
                        break
                    entry_path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
        return consumed


    def close(self) -> None:
        self._close_iterator()

def _iter_nul_records(payload: bytes) -> Iterator[bytes]:
    start = 0
    while start < len(payload):
        end = payload.find(b"\0", start)
        if end < 0:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed")
        if end > start:
            yield payload[start:end]
        start = end + 1


__all__ = ["CodingWorkspaceError", "CodingWorkspaceService"]
