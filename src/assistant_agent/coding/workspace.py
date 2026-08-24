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
_ANALYSIS_REAPER_TOMBSTONE_PREFIX = ".analysis-reap-"
_ANALYSIS_REAPER_PROGRESS_FILE = ".analysis-cleanup-progress.json"
_ANALYSIS_REAPER_POISON_PREFIX = ".analysis-reap-poison-"
_ANALYSIS_REAPER_METADATA_MAX_BYTES = 1_024
_ANALYSIS_REAPER_POISON_LIMIT = 64
_ANALYSIS_REAPER_POISON_COOLDOWN_SECONDS = 300.0
_ANALYSIS_REAPER_MAX_DEPTH = 128
_REAPER_TIME_BUDGET_SECONDS = 0.25
_REAPER_MAX_TIME_BUDGET_SECONDS = 1.0
_REAPER_DEFAULT_WORKSPACE_BUDGET = 32
_REAPER_DEFAULT_CHILD_BUDGET = 64
_ANALYSIS_INDEX_ENTRY_MAX_BYTES = 1_152


class CodingWorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message or 'coding workspace operation failed'}")


class _AnalysisSnapshotManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=1_024)
    kind: Literal["directory", "file"]
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    mode: int = Field(ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    content_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


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
    materialization_digest: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    materialization_manifest: tuple[_AnalysisSnapshotManifestEntry, ...] = ()
    status: CodingStatusResult
    diff: CodingDiffResult
    active_lease: bool = True


@dataclass(frozen=True, slots=True)
class _AnalysisSnapshotReadView:
    workspace: CodingWorkspace
    metadata: _AnalysisSnapshotMetadata
    tree_descriptor: int
    manifest: Mapping[str, _AnalysisSnapshotManifestEntry]


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
        self._cleanup_lock = threading.Lock()
        self._reaper_cursor_lock = threading.RLock()
        self._reaper_cursors: OrderedDict[str, _ReaperCursor] = OrderedDict()
        self._hierarchical_reaper = _HierarchicalReaperTraversal(
            self.config.workspace_root,
            child_directories=(),
        )
        self._incremental_reaper_deletion: _IncrementalTombstoneDeletion | None = None
        self._snapshot_reaper_deny: OrderedDict[str, float] = OrderedDict()

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
                        self._verify_analysis_snapshot_materialization(
                            existing.snapshot,
                            existing,
                            published_root,
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
                self._make_analysis_tree_read_only(tree_root)
                manifest_digest, materialization_manifest = (
                    self._analysis_snapshot_materialization_manifest(
                        temporary_root,
                        error_code="coding_analysis_snapshot_failed",
                    )
                )
                metadata = metadata.model_copy(
                    update={
                        "materialization_manifest": materialization_manifest,
                    }
                )
                metadata = metadata.model_copy(
                    update={
                        "materialization_digest": (
                            self._analysis_snapshot_materialization_mac(
                                snapshot,
                                metadata,
                                manifest_digest,
                            )
                        )
                    }
                )
                self._write_analysis_snapshot_metadata(
                    temporary_root / _ANALYSIS_SNAPSHOT_METADATA_FILE,
                    metadata,
                )
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
    ) -> _AnalysisSnapshotMetadata:
        """Validate a checkpoint binding without recreating or renewing it."""

        snapshot_root = self._analysis_snapshot_root(snapshot)
        with self._lock(snapshot.workspace_ref):
            try:
                snapshot_details = snapshot_root.lstat()
            except FileNotFoundError as exc:
                raise CodingWorkspaceError(
                    "coding_analysis_snapshot_missing"
                ) from exc
            except OSError as exc:
                raise CodingWorkspaceError(
                    "coding_analysis_snapshot_mismatch"
                ) from exc
            if not stat.S_ISDIR(snapshot_details.st_mode):
                raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
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
            self._verify_analysis_snapshot_materialization(
                snapshot,
                metadata,
                snapshot_root,
            )
            return metadata

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
        with self._analysis_snapshot_read_workspace(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        ) as snapshot_view:
            return self._list_analysis_snapshot_manifest(
                snapshot_view,
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
        with self._analysis_snapshot_read_workspace(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        ) as snapshot_view:
            return self._search_analysis_snapshot_manifest(
                snapshot_view,
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
        with self._analysis_snapshot_read_workspace(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        ) as snapshot_view:
            return self._read_analysis_snapshot_manifest(
                snapshot_view,
                path,
                start_line=start_line,
                end_line=end_line,
            )

    def status_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> CodingStatusResult:
        with self._analysis_snapshot_read_workspace(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        ) as snapshot_view:
            return snapshot_view.metadata.status

    def diff_analysis_snapshot(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> CodingDiffResult:
        with self._analysis_snapshot_read_workspace(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        ) as snapshot_view:
            return snapshot_view.metadata.diff

    @contextmanager
    def _analysis_snapshot_read_workspace(
        self,
        snapshot: CodingAnalysisSnapshot,
        *,
        identity: str,
        thread_id: str,
        workspace: CodingWorkspace,
    ) -> Iterator[_AnalysisSnapshotReadView]:
        resolved = self.resolve_analysis_snapshot(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
        )
        tree_root = (
            self._analysis_snapshot_root(snapshot)
            / _ANALYSIS_SNAPSHOT_TREE_DIR
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        tree_descriptor = -1
        try:
            tree_descriptor = os.open(tree_root, flags)
            opened_tree = os.fstat(tree_descriptor)
        except OSError as exc:
            if tree_descriptor >= 0:
                os.close(tree_descriptor)
            raise CodingWorkspaceError(
                "coding_analysis_snapshot_mismatch"
            ) from exc

        def confirm_open_tree_is_current() -> None:
            try:
                current_tree = tree_root.lstat()
            except OSError as exc:
                raise CodingWorkspaceError(
                    "coding_analysis_snapshot_mismatch"
                ) from exc
            if (
                not stat.S_ISDIR(opened_tree.st_mode)
                or not stat.S_ISDIR(current_tree.st_mode)
                or opened_tree.st_dev != current_tree.st_dev
                or opened_tree.st_ino != current_tree.st_ino
            ):
                raise CodingWorkspaceError(
                    "coding_analysis_snapshot_mismatch"
                )

        try:
            metadata = self.validate_analysis_snapshot(
                snapshot,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
                require_active=True,
            )
            confirm_open_tree_is_current()
            manifest = {
                entry.path: entry
                for entry in metadata.materialization_manifest
            }
            if len(manifest) != len(metadata.materialization_manifest):
                raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
            yield _AnalysisSnapshotReadView(
                workspace=resolved.model_copy(
                    update={"root": Path(f"/proc/self/fd/{tree_descriptor}")}
                ),
                metadata=metadata,
                tree_descriptor=tree_descriptor,
                manifest=manifest,
            )
            self.validate_analysis_snapshot(
                snapshot,
                identity=identity,
                thread_id=thread_id,
                workspace=workspace,
                require_active=True,
            )
            confirm_open_tree_is_current()
        finally:
            os.close(tree_descriptor)

    def _normalize_analysis_snapshot_path(
        self,
        raw_path: str,
        *,
        allow_root: bool,
    ) -> str:
        normalized = str(raw_path).strip()
        if allow_root and normalized in {"", "."}:
            return ""
        relative = Path(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise CodingWorkspaceError("path_invalid")
        relative_posix = relative.as_posix()
        if any(
            fnmatchcase(relative_posix, pattern)
            for pattern in self.policy.protected_globs
        ):
            raise CodingWorkspaceError("path_protected")
        return relative_posix

    @staticmethod
    def _analysis_snapshot_entry_matches(
        entry: _AnalysisSnapshotManifestEntry,
        details: os.stat_result,
    ) -> bool:
        expected_type = (
            stat.S_ISDIR(details.st_mode)
            if entry.kind == "directory"
            else stat.S_ISREG(details.st_mode)
        )
        return (
            expected_type
            and entry.device == details.st_dev
            and entry.inode == details.st_ino
            and entry.mode == stat.S_IMODE(details.st_mode)
            and (
                entry.kind == "directory"
                or entry.size_bytes == details.st_size
            )
        )

    def _open_analysis_snapshot_manifest_directory(
        self,
        view: _AnalysisSnapshotReadView,
        components: tuple[str, ...],
    ) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.dup(view.tree_descriptor)
        try:
            traversed: list[str] = []
            for component in components:
                traversed.append(component)
                expected = view.manifest.get("/".join(traversed))
                if expected is None or expected.kind != "directory":
                    raise CodingWorkspaceError(
                        "coding_analysis_snapshot_mismatch"
                    )
                initial = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                opened = os.open(component, flags, dir_fd=descriptor)
                opened_details = os.fstat(opened)
                if (
                    not self._analysis_snapshot_entry_matches(expected, initial)
                    or not self._analysis_snapshot_entry_matches(
                        expected,
                        opened_details,
                    )
                ):
                    os.close(opened)
                    raise CodingWorkspaceError(
                        "coding_analysis_snapshot_mismatch"
                    )
                os.close(descriptor)
                descriptor = opened
            return descriptor
        except CodingWorkspaceError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            raise CodingWorkspaceError(
                "coding_analysis_snapshot_mismatch"
            ) from exc

    def _read_analysis_snapshot_manifest_entry(
        self,
        view: _AnalysisSnapshotReadView,
        entry: _AnalysisSnapshotManifestEntry,
    ) -> bytes:
        if (
            entry.kind != "file"
            or entry.size_bytes is None
            or entry.content_digest is None
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
        components = tuple(Path(entry.path).parts)
        parent_descriptor = self._open_analysis_snapshot_manifest_directory(
            view,
            components[:-1],
        )
        descriptor = -1
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            initial = os.stat(
                components[-1],
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not self._analysis_snapshot_entry_matches(entry, initial):
                raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
            descriptor = os.open(
                components[-1],
                flags,
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            if not self._analysis_snapshot_entry_matches(entry, opened):
                raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
            content = bytearray()
            content_digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                content.extend(chunk)
                content_digest.update(chunk)
                if len(content) > entry.size_bytes:
                    raise CodingWorkspaceError(
                        "coding_analysis_snapshot_mismatch"
                    )
            confirmed = os.fstat(descriptor)
            if (
                not self._analysis_snapshot_entry_matches(entry, confirmed)
                or len(content) != entry.size_bytes
                or not hmac.compare_digest(
                    content_digest.hexdigest(),
                    entry.content_digest,
                )
            ):
                raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
            return bytes(content)
        except CodingWorkspaceError:
            raise
        except OSError as exc:
            raise CodingWorkspaceError(
                "coding_analysis_snapshot_mismatch"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _list_analysis_snapshot_manifest(
        self,
        view: _AnalysisSnapshotReadView,
        *,
        path: str,
        depth: int,
        cursor: int,
        limit: int,
    ) -> CodingListResult:
        if depth < 1 or depth > 8 or cursor < 0 or limit < 1 or limit > 200:
            raise CodingWorkspaceError("invalid_tool_input")
        normalized = self._normalize_analysis_snapshot_path(
            path,
            allow_root=True,
        )
        if normalized:
            root_entry = view.manifest.get(normalized)
            if root_entry is None or root_entry.kind != "directory":
                raise CodingWorkspaceError("path_invalid")
        base_depth = len(Path(normalized).parts) if normalized else 0
        prefix = f"{normalized}/" if normalized else ""
        entries: list[CodingListEntry] = []
        for entry in sorted(view.manifest.values(), key=lambda item: item.path):
            if prefix and not entry.path.startswith(prefix):
                continue
            current_depth = len(Path(entry.path).parts) - base_depth
            if current_depth < 1 or current_depth > depth:
                continue
            if entry.kind == "directory":
                entries.append(CodingListEntry(path=entry.path, kind="directory"))
            else:
                if entry.size_bytes is None:
                    raise CodingWorkspaceError(
                        "coding_analysis_snapshot_mismatch"
                    )
                entries.append(
                    CodingListEntry(
                        path=entry.path,
                        kind="file",
                        size_bytes=entry.size_bytes,
                    )
                )
        page = tuple(entries[cursor : cursor + limit])
        next_cursor = (
            cursor + len(page)
            if cursor + len(page) < len(entries)
            else None
        )
        return CodingListResult(entries=page, next_cursor=next_cursor)

    def _read_analysis_snapshot_manifest(
        self,
        view: _AnalysisSnapshotReadView,
        path: str,
        *,
        start_line: int,
        end_line: int,
    ) -> CodingReadResult:
        if start_line < 1 or end_line < start_line or end_line - start_line > 2_000:
            raise CodingWorkspaceError("invalid_tool_input")
        normalized = self._normalize_analysis_snapshot_path(
            path,
            allow_root=False,
        )
        entry = view.manifest.get(normalized)
        if entry is None or entry.kind != "file":
            raise CodingWorkspaceError("path_invalid")
        raw_content = self._read_analysis_snapshot_manifest_entry(view, entry)
        try:
            lines = raw_content.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise CodingWorkspaceError("file_encoding_unsupported") from exc
        selected = lines[start_line - 1 : end_line]
        actual_end = start_line + len(selected) - 1 if selected else start_line - 1
        return CodingReadResult(
            path=normalized,
            content="".join(selected),
            start_line=start_line,
            end_line=actual_end,
            total_lines=len(lines),
            next_line=actual_end + 1 if actual_end < len(lines) else None,
        )

    def _search_analysis_snapshot_manifest(
        self,
        view: _AnalysisSnapshotReadView,
        *,
        query: str,
        paths: tuple[str, ...],
        globs: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> CodingSearchResult:
        if not query or len(query) > 1_000 or cursor < 0 or limit < 1 or limit > 200:
            raise CodingWorkspaceError("invalid_tool_input")
        roots: list[tuple[str, Literal["directory", "file"]]] = []
        for raw_path in paths or ("",):
            normalized = self._normalize_analysis_snapshot_path(
                raw_path,
                allow_root=True,
            )
            if not normalized:
                roots.append(("", "directory"))
                continue
            entry = view.manifest.get(normalized)
            if entry is None:
                raise CodingWorkspaceError("path_invalid")
            roots.append((normalized, entry.kind))

        candidates: dict[str, _AnalysisSnapshotManifestEntry] = {}
        for entry in view.manifest.values():
            if entry.kind != "file":
                continue
            for root_path, root_kind in roots:
                if (
                    (root_kind == "file" and entry.path == root_path)
                    or (
                        root_kind == "directory"
                        and (
                            not root_path
                            or entry.path.startswith(f"{root_path}/")
                        )
                    )
                ):
                    candidates[entry.path] = entry
                    break

        matches: list[CodingSearchMatch] = []
        for relative, entry in sorted(candidates.items()):
            if globs and not any(
                fnmatchcase(relative, pattern) for pattern in globs
            ):
                continue
            raw_content = self._read_analysis_snapshot_manifest_entry(view, entry)
            try:
                lines = raw_content.decode("utf-8").splitlines()
            except UnicodeDecodeError:
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
        next_cursor = (
            cursor + len(page)
            if cursor + len(page) < len(matches)
            else None
        )
        return CodingSearchResult(matches=page, next_cursor=next_cursor)

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

    def _verify_analysis_snapshot_materialization(
        self,
        snapshot: CodingAnalysisSnapshot,
        metadata: _AnalysisSnapshotMetadata,
        snapshot_root: Path,
    ) -> None:
        actual = self._analysis_snapshot_materialization_digest(
            snapshot,
            metadata,
            snapshot_root,
            error_code="coding_analysis_snapshot_mismatch",
        )
        if not hmac.compare_digest(metadata.materialization_digest, actual):
            raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")

    def _analysis_snapshot_materialization_digest(
        self,
        snapshot: CodingAnalysisSnapshot,
        metadata: _AnalysisSnapshotMetadata,
        snapshot_root: Path,
        *,
        error_code: str,
    ) -> str:
        manifest_digest, manifest = self._analysis_snapshot_materialization_manifest(
            snapshot_root,
            error_code=error_code,
        )
        if metadata.materialization_manifest != manifest:
            raise CodingWorkspaceError(error_code)
        return self._analysis_snapshot_materialization_mac(
            snapshot,
            metadata,
            manifest_digest,
        )

    def _analysis_snapshot_materialization_mac(
        self,
        snapshot: CodingAnalysisSnapshot,
        metadata: _AnalysisSnapshotMetadata,
        manifest_digest: str,
    ) -> str:
        payload = json.dumps(
            {
                "schema": "coding-analysis-materialization-v1",
                "snapshot": snapshot.model_dump(mode="json"),
                "identity_digest": metadata.identity_digest,
                "thread_digest": metadata.thread_digest,
                "workspace_digest": metadata.workspace_digest,
                "repo_id": metadata.repo_id,
                "tree_object": metadata.tree_object,
                "status": metadata.status.model_dump(mode="json"),
                "diff": metadata.diff.model_dump(mode="json"),
                "materialization_manifest": [
                    entry.model_dump(mode="json")
                    for entry in metadata.materialization_manifest
                ],
                "manifest_digest": manifest_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret(), payload, hashlib.sha256).hexdigest()

    def _analysis_snapshot_materialization_manifest(
        self,
        snapshot_root: Path,
        *,
        error_code: str,
    ) -> tuple[str, tuple[_AnalysisSnapshotManifestEntry, ...]]:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        parent_descriptor = root_descriptor = tree_descriptor = -1
        budget = _AnalysisScanBudget(self.config)
        digest = hashlib.sha256()
        manifest: list[_AnalysisSnapshotManifestEntry] = []

        def fail(exc: BaseException | None = None) -> None:
            if exc is None:
                raise CodingWorkspaceError(error_code)
            raise CodingWorkspaceError(error_code) from exc

        def include(value: str | bytes | int) -> None:
            raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
            digest.update(struct.pack(">Q", len(raw)))
            digest.update(raw)

        def same_node(first: os.stat_result, second: os.stat_result) -> bool:
            return (
                first.st_dev == second.st_dev
                and first.st_ino == second.st_ino
                and first.st_mode == second.st_mode
                and first.st_size == second.st_size
                and first.st_mtime_ns == second.st_mtime_ns
                and first.st_ctime_ns == second.st_ctime_ns
            )

        def open_directory(components: tuple[str, ...]) -> int:
            descriptor = os.dup(tree_descriptor)
            try:
                for component in components:
                    opened = os.open(
                        component,
                        directory_flags,
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = opened
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

        try:
            parent_descriptor = os.open(snapshot_root.parent, directory_flags)
            initial_root = os.stat(
                snapshot_root.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            root_descriptor = os.open(
                snapshot_root.name,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            opened_root = os.fstat(root_descriptor)
            if not stat.S_ISDIR(opened_root.st_mode) or not same_node(
                initial_root,
                opened_root,
            ):
                fail()
            initial_tree = os.stat(
                _ANALYSIS_SNAPSHOT_TREE_DIR,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            tree_descriptor = os.open(
                _ANALYSIS_SNAPSHOT_TREE_DIR,
                directory_flags,
                dir_fd=root_descriptor,
            )
            opened_tree = os.fstat(tree_descriptor)
            if not stat.S_ISDIR(opened_tree.st_mode) or not same_node(
                initial_tree,
                opened_tree,
            ):
                fail()

            include("coding-analysis-materialized-tree-v1")
            for details in (opened_root, opened_tree):
                include(details.st_dev)
                include(details.st_ino)
                include(stat.S_IMODE(details.st_mode))

            budget.visit_directory()
            pending: list[tuple[str, ...]] = [()]
            while pending:
                components = pending.pop()
                directory_descriptor = open_directory(components)
                try:
                    before = os.fstat(directory_descriptor)
                    if not stat.S_ISDIR(before.st_mode):
                        fail()
                    names: list[str] = []
                    with os.scandir(directory_descriptor) as entries:
                        for entry in entries:
                            budget.visit_entry()
                            names.append(entry.name)
                    names.sort()
                    after = os.fstat(directory_descriptor)
                    if not same_node(before, after):
                        fail()
                finally:
                    os.close(directory_descriptor)

                for name in reversed(names):
                    path_components = (*components, name)
                    relative_path = "/".join(path_components)
                    try:
                        encoded_path = relative_path.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        fail(exc)
                    if (
                        len(encoded_path) > 1_024
                        or any(
                            character in relative_path
                            for character in ("\x00", "\n", "\r")
                        )
                    ):
                        fail()
                    parent = open_directory(components)
                    try:
                        initial = os.stat(
                            name,
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                        if stat.S_ISDIR(initial.st_mode):
                            opened = os.open(name, directory_flags, dir_fd=parent)
                            try:
                                details = os.fstat(opened)
                            finally:
                                os.close(opened)
                            if not same_node(initial, details):
                                fail()
                            budget.visit_directory()
                            include("directory")
                            include(encoded_path)
                            include(details.st_dev)
                            include(details.st_ino)
                            include(stat.S_IMODE(details.st_mode))
                            manifest.append(
                                _AnalysisSnapshotManifestEntry(
                                    path=relative_path,
                                    kind="directory",
                                    device=details.st_dev,
                                    inode=details.st_ino,
                                    mode=stat.S_IMODE(details.st_mode),
                                )
                            )
                            pending.append(path_components)
                            continue
                        if not stat.S_ISREG(initial.st_mode):
                            fail()
                        budget.attempt_file(initial.st_size)
                        if initial.st_size > self.config.max_file_bytes:
                            fail()
                        descriptor = os.open(name, file_flags, dir_fd=parent)
                        try:
                            opened = os.fstat(descriptor)
                            if not same_node(initial, opened):
                                fail()
                            content_digest = hashlib.sha256()
                            total_bytes = 0
                            while True:
                                chunk = os.read(descriptor, 65_536)
                                if not chunk:
                                    break
                                total_bytes += len(chunk)
                                budget.consume_read(len(chunk))
                                if total_bytes > self.config.max_file_bytes:
                                    fail()
                                content_digest.update(chunk)
                            confirmed = os.fstat(descriptor)
                            if not same_node(opened, confirmed):
                                fail()
                        finally:
                            os.close(descriptor)
                        budget.include_file(total_bytes)
                        include("file")
                        include(encoded_path)
                        include(confirmed.st_dev)
                        include(confirmed.st_ino)
                        include(stat.S_IMODE(confirmed.st_mode))
                        include(total_bytes)
                        include(content_digest.digest())
                        manifest.append(
                            _AnalysisSnapshotManifestEntry(
                                path=relative_path,
                                kind="file",
                                device=confirmed.st_dev,
                                inode=confirmed.st_ino,
                                mode=stat.S_IMODE(confirmed.st_mode),
                                size_bytes=total_bytes,
                                content_digest=content_digest.hexdigest(),
                            )
                        )
                    finally:
                        os.close(parent)

            current_tree = os.stat(
                _ANALYSIS_SNAPSHOT_TREE_DIR,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            current_root = os.stat(
                snapshot_root.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not same_node(opened_tree, current_tree) or not same_node(
                opened_root,
                current_root,
            ):
                fail()
            return digest.hexdigest(), tuple(manifest)
        except CodingWorkspaceError:
            raise
        except (OSError, ValueError) as exc:
            fail(exc)
        finally:
            for descriptor in (
                tree_descriptor,
                root_descriptor,
                parent_descriptor,
            ):
                if descriptor >= 0:
                    os.close(descriptor)

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
        except (CodingWorkspaceError, OSError) as exc:
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
            if metadata.expires_at > self._clock():
                continue
            lock_path = management_root / _LOCK_FILE
            lock_path.touch(mode=0o600, exist_ok=True)
            with lock_path.open("a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
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
            shutil.rmtree(management_root, ignore_errors=False)

    def cleanup_expired_analysis_snapshots(
        self,
        *,
        max_workspaces: int = _REAPER_DEFAULT_WORKSPACE_BUDGET,
        max_snapshots_per_workspace: int = _REAPER_DEFAULT_CHILD_BUDGET,
    ) -> None:
        if max_workspaces <= 0 or max_snapshots_per_workspace <= 0:
            return
        with self._cleanup_lock:
            self._cleanup_expired_analysis_snapshots_locked(
                max_workspaces=max_workspaces,
                max_snapshots_per_workspace=max_snapshots_per_workspace,
            )

    def _cleanup_expired_analysis_snapshots_locked(
        self,
        *,
        max_workspaces: int,
        max_snapshots_per_workspace: int,
    ) -> None:
        root = self.config.workspace_root
        try:
            root_details = root.lstat()
        except OSError:
            root_details = None
        if root_details is None or not stat.S_ISDIR(root_details.st_mode):
            self._drop_reaper_cursors_under(root)
            self._close_incremental_snapshot_deletion()
            self._snapshot_reaper_deny.clear()
            return

        deadline = time.monotonic() + min(
            _REAPER_MAX_TIME_BUDGET_SECONDS,
            _REAPER_TIME_BUDGET_SECONDS * max(1, max_workspaces),
        )
        self._advance_incremental_snapshot_deletion(
            max_entries=max_snapshots_per_workspace,
            absolute_deadline=deadline,
        )
        if self._hierarchical_reaper.exhausted:
            self._hierarchical_reaper.restart()

        scanned_root_entries = 0
        restarted_root = False
        while scanned_root_entries < max_workspaces and time.monotonic() < deadline:
            entry = self._hierarchical_reaper.next_entry()
            if entry is None:
                if restarted_root:
                    break
                self._hierarchical_reaper.restart()
                restarted_root = True
                continue
            scanned_root_entries += 1
            if entry.kind != "workspace_start":
                continue
            management_root = entry.workspace
            remaining_slots = max(1, max_workspaces - scanned_root_entries + 1)
            now = time.monotonic()
            workspace_deadline = min(
                deadline,
                now + max(0.0, deadline - now) / remaining_slots,
            )
            try:
                self._cleanup_snapshot_workspace_slice(
                    management_root,
                    max_entries=max_snapshots_per_workspace,
                    absolute_deadline=workspace_deadline,
                )
            finally:
                self._hierarchical_reaper.skip_current_workspace()

    def _reap_analysis_snapshots_locked(
        self,
        management_root: Path,
        *,
        max_snapshots: int | None = None,
    ) -> None:
        self._cleanup_snapshot_workspace_slice(
            management_root,
            max_entries=(
                _REAPER_DEFAULT_CHILD_BUDGET
                if max_snapshots is None
                else max(0, max_snapshots)
            ),
            absolute_deadline=time.monotonic() + _REAPER_TIME_BUDGET_SECONDS,
        )

    def _validated_snapshot_management_root(
        self,
        management_root: Path,
    ) -> CodingWorkspaceMetadata | None:
        workspace_root = Path(os.path.abspath(os.fspath(self.config.workspace_root)))
        candidate = Path(os.path.abspath(os.fspath(management_root)))
        if candidate.parent != workspace_root or candidate.name != management_root.name:
            return None
        try:
            root_details = management_root.lstat()
            metadata_details = (management_root / _METADATA_FILE).lstat()
            repo_details = (management_root / _REPO_DIR).lstat()
        except OSError:
            return None
        if (
            not stat.S_ISDIR(root_details.st_mode)
            or not stat.S_ISREG(metadata_details.st_mode)
            or not stat.S_ISDIR(repo_details.st_mode)
        ):
            return None
        try:
            metadata = self._load_metadata(management_root / _METADATA_FILE)
        except CodingWorkspaceError:
            return None
        if (
            metadata.workspace_ref != management_root.name
            or metadata.repo_id not in self.config.repositories
            or self._management_root(metadata.workspace_ref) != management_root
        ):
            return None
        return metadata

    def _open_reaper_workspace_lock(self, management_root: Path):
        lock_path = management_root / _LOCK_FILE
        descriptor = -1
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                os.close(descriptor)
                return None
            handle = os.fdopen(descriptor, "a+")
            descriptor = -1
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
            return handle
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            return None

    def _load_snapshot_cleanup_cookie(
        self,
        management_root: Path,
        *,
        absolute_deadline: float,
    ) -> int:
        parent_descriptor = -1
        descriptor = -1
        payload = bytearray()
        try:
            parent_descriptor = os.open(
                management_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            descriptor = os.open(
                _ANALYSIS_REAPER_PROGRESS_FILE,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_size > _ANALYSIS_REAPER_METADATA_MAX_BYTES
            ):
                return 0
            while len(payload) <= _ANALYSIS_REAPER_METADATA_MAX_BYTES:
                if time.monotonic() >= absolute_deadline:
                    return 0
                try:
                    chunk = os.read(
                        descriptor,
                        min(
                            256,
                            _ANALYSIS_REAPER_METADATA_MAX_BYTES + 1 - len(payload),
                        ),
                    )
                except BlockingIOError:
                    return 0
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > _ANALYSIS_REAPER_METADATA_MAX_BYTES:
                return 0
        except OSError:
            return 0
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
        try:
            decoded = json.loads(payload.decode("utf-8"))
            if not isinstance(decoded, dict):
                return 0
            if set(decoded) != {"schema_version", "phase", "cookie"}:
                return 0
            schema_version = decoded["schema_version"]
            phase = decoded["phase"]
            cookie = decoded["cookie"]
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
            return 0
        if type(schema_version) is not int or schema_version != 1:
            return 0
        if not isinstance(phase, str) or phase != "snapshot":
            return 0
        if type(cookie) is not int:
            return 0
        if not 0 <= cookie <= (2**63 - 1):
            return 0
        return cookie

    def _write_snapshot_cleanup_cookie(
        self,
        management_root: Path,
        *,
        cookie: int,
        absolute_deadline: float,
    ) -> None:
        if not 0 <= cookie <= (2**63 - 1):
            return
        temporary = f".{_ANALYSIS_REAPER_PROGRESS_FILE}.{os.getpid()}.{time.monotonic_ns()}"
        payload = json.dumps(
            {"schema_version": 1, "phase": "snapshot", "cookie": cookie},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parent_descriptor = -1
        descriptor = -1
        try:
            parent_descriptor = os.open(
                management_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            view = memoryview(payload)
            while view:
                if time.monotonic() >= absolute_deadline:
                    return
                written = os.write(descriptor, view)
                if written <= 0:
                    return
                view = view[written:]
            if time.monotonic() >= absolute_deadline:
                return
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                existing = os.stat(
                    _ANALYSIS_REAPER_PROGRESS_FILE,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                return
            os.replace(
                temporary,
                _ANALYSIS_REAPER_PROGRESS_FILE,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        except OSError:
            return
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_descriptor >= 0:
                try:
                    os.unlink(temporary, dir_fd=parent_descriptor)
                except OSError:
                    pass
                os.close(parent_descriptor)

    def _cleanup_snapshot_workspace_slice(
        self,
        management_root: Path,
        *,
        max_entries: int,
        absolute_deadline: float,
    ) -> None:
        if max_entries <= 0 or time.monotonic() >= absolute_deadline:
            return
        if self._validated_snapshot_management_root(management_root) is None:
            return
        handle = self._open_reaper_workspace_lock(management_root)
        if handle is None:
            return
        try:
            if self._validated_snapshot_management_root(management_root) is None:
                return
            snapshots_root = management_root / _ANALYSIS_SNAPSHOTS_DIR
            try:
                snapshots_details = snapshots_root.lstat()
            except OSError:
                return
            if not stat.S_ISDIR(snapshots_details.st_mode):
                return
            cookie = self._load_snapshot_cleanup_cookie(
                management_root,
                absolute_deadline=absolute_deadline,
            )
            try:
                page = _read_persistent_directory_page(
                    snapshots_root,
                    cookie=cookie,
                    max_entries=max_entries,
                    absolute_deadline=absolute_deadline,
                )
            except OSError:
                return
            for entry in page.entries:
                if time.monotonic() >= absolute_deadline:
                    break
                self._reap_snapshot_path_bounded(entry.path)
            self._write_snapshot_cleanup_cookie(
                management_root,
                cookie=0 if page.done else page.cookie,
                absolute_deadline=absolute_deadline,
            )
        finally:
            handle.close()

    def _managed_snapshot_path(self, path: Path) -> bool:
        snapshots_root = path.parent
        management_root = snapshots_root.parent
        if snapshots_root.name != _ANALYSIS_SNAPSHOTS_DIR:
            return False
        if self._validated_snapshot_management_root(management_root) is None:
            return False
        try:
            snapshots_details = snapshots_root.lstat()
            path_details = path.lstat()
        except OSError:
            return False
        return stat.S_ISDIR(snapshots_details.st_mode) and stat.S_ISDIR(path_details.st_mode)

    def _snapshot_reaper_deny_key(self, path: Path) -> str:
        return os.path.abspath(os.fspath(path))

    def _snapshot_poison_marker_name(self, path: Path) -> str:
        return f"{_ANALYSIS_REAPER_POISON_PREFIX}{_digest(path.name)}.json"

    def _remember_snapshot_reaper_denial(self, path: Path) -> None:
        key = self._snapshot_reaper_deny_key(path)
        self._snapshot_reaper_deny[key] = (
            time.monotonic() + _ANALYSIS_REAPER_POISON_COOLDOWN_SECONDS
        )
        self._snapshot_reaper_deny.move_to_end(key)
        while len(self._snapshot_reaper_deny) > _ANALYSIS_REAPER_POISON_LIMIT:
            self._snapshot_reaper_deny.popitem(last=False)

    def _has_snapshot_poison_marker(self, path: Path) -> bool:
        parent_descriptor = -1
        descriptor = -1
        try:
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            descriptor = os.open(
                self._snapshot_poison_marker_name(path),
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_size > _ANALYSIS_REAPER_METADATA_MAX_BYTES
            ):
                return False
            payload = os.read(descriptor, _ANALYSIS_REAPER_METADATA_MAX_BYTES + 1)
            if len(payload) > _ANALYSIS_REAPER_METADATA_MAX_BYTES:
                return False
            decoded = json.loads(payload.decode("utf-8"))
            if not isinstance(decoded, dict):
                return False
            if set(decoded) != {
                "schema_version",
                "tombstone_name",
                "device",
                "inode",
            }:
                return False
            schema_version = decoded["schema_version"]
            tombstone_name = decoded["tombstone_name"]
            device = decoded["device"]
            inode = decoded["inode"]
            return (
                type(schema_version) is int
                and schema_version == 1
                and isinstance(tombstone_name, str)
                and tombstone_name == path.name
                and type(device) is int
                and 0 <= device <= (2**64 - 1)
                and type(inode) is int
                and 0 <= inode <= (2**64 - 1)
            )
        except (
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
        ):
            return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_descriptor >= 0:
                os.close(parent_descriptor)

    def _snapshot_reaper_denied(self, path: Path) -> bool:
        key = self._snapshot_reaper_deny_key(path)
        denied_until = self._snapshot_reaper_deny.get(key)
        if denied_until is not None:
            if denied_until > time.monotonic():
                self._snapshot_reaper_deny.move_to_end(key)
                return True
            self._snapshot_reaper_deny.pop(key, None)
        if self._has_snapshot_poison_marker(path):
            self._remember_snapshot_reaper_denial(path)
            return True
        return False

    def _poison_snapshot_deletion(
        self,
        deletion: _IncrementalTombstoneDeletion,
    ) -> None:
        path = deletion.tombstone_root
        self._remember_snapshot_reaper_denial(path)
        parent_descriptor = -1
        descriptor = -1
        temporary = (
            f".{self._snapshot_poison_marker_name(path)}."
            f"{os.getpid()}.{time.monotonic_ns()}"
        )
        payload = json.dumps(
            {
                "schema_version": 1,
                "tombstone_name": deletion.tombstone_name,
                "device": deletion.root_device,
                "inode": deletion.root_inode,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            parent_descriptor = os.open(
                deletion.snapshots_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            parent_details = os.fstat(parent_descriptor)
            if (
                parent_details.st_dev != deletion.parent_device
                or parent_details.st_ino != deletion.parent_inode
            ):
                return
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    return
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            marker_name = self._snapshot_poison_marker_name(path)
            try:
                existing = os.stat(
                    marker_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                return
            os.replace(
                temporary,
                marker_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        except OSError:
            return
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_descriptor >= 0:
                try:
                    os.unlink(temporary, dir_fd=parent_descriptor)
                except OSError:
                    pass
                os.close(parent_descriptor)

    def _schedule_incremental_removal(self, path: Path, *, advance: bool) -> Path | None:
        if not self._managed_snapshot_path(path):
            return None
        if self._snapshot_reaper_denied(path):
            return None
        current = self._incremental_reaper_deletion
        if current is not None:
            if current.done:
                current.close()
                self._incremental_reaper_deletion = None
            else:
                if advance:
                    self._advance_incremental_snapshot_deletion(
                        max_entries=_REAPER_DEFAULT_CHILD_BUDGET,
                        absolute_deadline=time.monotonic() + _REAPER_TIME_BUDGET_SECONDS,
                    )
                if self._incremental_reaper_deletion is not None:
                    return None
        try:
            if path.name.startswith(_ANALYSIS_REAPER_TOMBSTONE_PREFIX):
                deletion = _IncrementalTombstoneDeletion.from_existing(path)
            else:
                deletion = _IncrementalTombstoneDeletion.rename(path)
        except CodingWorkspaceError:
            self._remember_snapshot_reaper_denial(path)
            return None
        if deletion.failed_closed:
            self._poison_snapshot_deletion(deletion)
            deletion.close()
            return None
        self._drop_reaper_cursors_under(path)
        self._incremental_reaper_deletion = deletion
        if advance:
            self._advance_incremental_snapshot_deletion(
                max_entries=_REAPER_DEFAULT_CHILD_BUDGET,
                absolute_deadline=time.monotonic() + _REAPER_TIME_BUDGET_SECONDS,
            )
        return deletion.tombstone_root

    def _advance_incremental_snapshot_deletion(
        self,
        *,
        max_entries: int,
        absolute_deadline: float,
    ) -> None:
        deletion = self._incremental_reaper_deletion
        if deletion is None or max_entries <= 0:
            return
        try:
            deletion.step(
                max_entries=max_entries,
                absolute_deadline=absolute_deadline,
            )
        except OSError:
            deletion.close()
            return
        if deletion.failed_closed:
            self._poison_snapshot_deletion(deletion)
            deletion.close()
            self._incremental_reaper_deletion = None
            return
        if deletion.done:
            deletion.close()
            self._incremental_reaper_deletion = None

    def _close_incremental_snapshot_deletion(self) -> None:
        deletion = self._incremental_reaper_deletion
        self._incremental_reaper_deletion = None
        if deletion is not None:
            deletion.close()

    def _reap_snapshot_path_bounded(self, snapshot_root: Path) -> None:
        try:
            details = snapshot_root.lstat()
            if not stat.S_ISDIR(details.st_mode):
                return
            if snapshot_root.name.startswith(_ANALYSIS_REAPER_TOMBSTONE_PREFIX):
                self._schedule_incremental_removal(snapshot_root, advance=False)
                return
            if snapshot_root.name.startswith(_ANALYSIS_BUILD_PREFIX):
                self._schedule_incremental_removal(snapshot_root, advance=False)
                return
            if snapshot_root.name.startswith(_ANALYSIS_QUARANTINE_PREFIX):
                quarantined_at = datetime.fromtimestamp(details.st_mtime, timezone.utc)
                if (
                    quarantined_at + timedelta(seconds=_ANALYSIS_QUARANTINE_SECONDS)
                    <= self._clock()
                ):
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

    def _quarantine_analysis_snapshot(self, snapshot_root: Path) -> None:
        if not self._managed_snapshot_path(snapshot_root):
            return
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
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
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
        self._close_incremental_snapshot_deletion()
        self._snapshot_reaper_deny.clear()

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
                    self.cleanup_expired_analysis_snapshots,
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
                self.cleanup_expired_analysis_snapshots,
                max_workspaces=_REAPER_DEFAULT_WORKSPACE_BUDGET,
                max_snapshots_per_workspace=_REAPER_DEFAULT_CHILD_BUDGET,
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


@dataclass(frozen=True, slots=True)
class _FdDirectoryEntry:
    name: str
    directory_type: int


@dataclass(frozen=True, slots=True)
class _FdDirectoryPage:
    entries: tuple[_FdDirectoryEntry, ...]
    cookie: int
    done: bool


@dataclass(slots=True)
class _SnapshotDeletionFrame:
    components: tuple[str, ...]
    cookie: int
    device: int
    inode: int


def _read_fd_directory_page(
    directory_fd: int,
    *,
    cookie: int,
    max_entries: int,
    absolute_deadline: float,
) -> _FdDirectoryPage:
    if max_entries <= 0 or time.monotonic() >= absolute_deadline:
        return _FdDirectoryPage((), cookie, False)
    if _GETDENTS64 is None:
        raise OSError("getdents64 is unavailable on this platform")
    os.lseek(directory_fd, cookie, os.SEEK_SET)
    entries: list[_FdDirectoryEntry] = []
    current_cookie = cookie
    while len(entries) < max_entries:
        if time.monotonic() >= absolute_deadline:
            return _FdDirectoryPage(tuple(entries), current_cookie, False)
        buffer = ctypes.create_string_buffer(4_096)
        received = _GETDENTS64(directory_fd, buffer, len(buffer))
        if received < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        if received == 0:
            return _FdDirectoryPage(tuple(entries), current_cookie, True)
        offset = 0
        while offset < received:
            if time.monotonic() >= absolute_deadline:
                return _FdDirectoryPage(tuple(entries), current_cookie, False)
            if received - offset < 19:
                raise OSError("invalid getdents64 record")
            _inode, next_cookie, record_length, directory_type = struct.unpack_from(
                "=QqHB",
                buffer.raw,
                offset,
            )
            if record_length < 19 or offset + record_length > received or next_cookie < 0:
                raise OSError("invalid getdents64 record")
            name_start = offset + 19
            name_end = buffer.raw.find(b"\0", name_start, offset + record_length)
            if name_end < 0:
                raise OSError("invalid getdents64 name")
            raw_name = buffer.raw[name_start:name_end]
            current_cookie = int(next_cookie)
            offset += record_length
            if raw_name in {b"", b".", b".."}:
                continue
            name = os.fsdecode(raw_name)
            if not name or name in {".", ".."} or "/" in name or "\0" in name:
                raise OSError("unsafe directory entry")
            entries.append(
                _FdDirectoryEntry(
                    name=name,
                    directory_type=int(directory_type),
                )
            )
            if len(entries) >= max_entries:
                return _FdDirectoryPage(tuple(entries), current_cookie, False)
    return _FdDirectoryPage(tuple(entries), current_cookie, False)


class _IncrementalTombstoneDeletion:
    def __init__(
        self,
        *,
        snapshots_root: Path,
        parent_device: int,
        parent_inode: int,
        tombstone_name: str,
        root_device: int,
        root_inode: int,
    ) -> None:
        self.snapshots_root = snapshots_root
        self.parent_device = parent_device
        self.parent_inode = parent_inode
        self.tombstone_name = tombstone_name
        self.root_device = root_device
        self.root_inode = root_inode
        self.tombstone_root = snapshots_root / tombstone_name
        self.done = False
        self.failed_closed = False
        self._frames = [
            _SnapshotDeletionFrame(
                components=(),
                cookie=0,
                device=root_device,
                inode=root_inode,
            )
        ]
        self._before_mutation_hook: (
            Callable[[str, tuple[str, ...], str], None] | None
        ) = None

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

    @classmethod
    def rename(cls, source: Path) -> _IncrementalTombstoneDeletion:
        if source.parent.name != _ANALYSIS_SNAPSHOTS_DIR:
            raise CodingWorkspaceError("coding_analysis_snapshot_failed")
        parent_descriptor = -1
        try:
            parent_descriptor = os.open(source.parent, cls._directory_flags())
            parent_details = os.fstat(parent_descriptor)
            source_details = os.stat(
                source.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(source_details.st_mode):
                raise CodingWorkspaceError("coding_analysis_snapshot_failed")
            for attempt in range(8):
                tombstone_name = (
                    f"{_ANALYSIS_REAPER_TOMBSTONE_PREFIX}{source.name}-"
                    f"{os.getpid()}-{time.monotonic_ns()}-{attempt}"
                )
                try:
                    os.stat(
                        tombstone_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    continue
                try:
                    os.rename(
                        source.name,
                        tombstone_name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                except FileExistsError:
                    continue
                tombstone_details = os.stat(
                    tombstone_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(tombstone_details.st_mode)
                    or tombstone_details.st_dev != source_details.st_dev
                    or tombstone_details.st_ino != source_details.st_ino
                ):
                    raise CodingWorkspaceError("coding_analysis_snapshot_failed")
                return cls(
                    snapshots_root=source.parent,
                    parent_device=parent_details.st_dev,
                    parent_inode=parent_details.st_ino,
                    tombstone_name=tombstone_name,
                    root_device=tombstone_details.st_dev,
                    root_inode=tombstone_details.st_ino,
                )
        except (CodingWorkspaceError, OSError) as exc:
            if isinstance(exc, CodingWorkspaceError):
                raise
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
        raise CodingWorkspaceError("coding_analysis_snapshot_failed")

    @classmethod
    def from_existing(cls, tombstone: Path) -> _IncrementalTombstoneDeletion:
        if (
            tombstone.parent.name != _ANALYSIS_SNAPSHOTS_DIR
            or not tombstone.name.startswith(_ANALYSIS_REAPER_TOMBSTONE_PREFIX)
        ):
            raise CodingWorkspaceError("coding_analysis_snapshot_failed")
        parent_descriptor = -1
        try:
            parent_descriptor = os.open(tombstone.parent, cls._directory_flags())
            parent_details = os.fstat(parent_descriptor)
            root_details = os.stat(
                tombstone.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(root_details.st_mode):
                raise CodingWorkspaceError("coding_analysis_snapshot_failed")
            return cls(
                snapshots_root=tombstone.parent,
                parent_device=parent_details.st_dev,
                parent_inode=parent_details.st_ino,
                tombstone_name=tombstone.name,
                root_device=root_details.st_dev,
                root_inode=root_details.st_ino,
            )
        except (CodingWorkspaceError, OSError) as exc:
            if isinstance(exc, CodingWorkspaceError):
                raise
            raise CodingWorkspaceError("coding_analysis_snapshot_failed") from exc
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)

    def _fail_closed(self) -> None:
        self.failed_closed = True

    def _open_snapshots_root(self) -> int:
        try:
            descriptor = os.open(self.snapshots_root, self._directory_flags())
            details = os.fstat(descriptor)
            if (
                details.st_dev != self.parent_device
                or details.st_ino != self.parent_inode
            ):
                os.close(descriptor)
                self._fail_closed()
                return -1
            return descriptor
        except OSError:
            self._fail_closed()
            return -1

    def _open_current_directory(
        self,
        snapshots_descriptor: int,
    ) -> tuple[int, int, str] | None:
        parent_descriptor = os.dup(snapshots_descriptor)
        try:
            for index, frame in enumerate(self._frames):
                name = self.tombstone_name if index == 0 else frame.components[-1]
                details = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(details.st_mode)
                    or details.st_dev != frame.device
                    or details.st_ino != frame.inode
                ):
                    self._fail_closed()
                    return None
                child_descriptor = os.open(
                    name,
                    self._directory_flags(),
                    dir_fd=parent_descriptor,
                )
                opened = os.fstat(child_descriptor)
                if (
                    opened.st_dev != details.st_dev
                    or opened.st_ino != details.st_ino
                ):
                    os.close(child_descriptor)
                    self._fail_closed()
                    return None
                if index == len(self._frames) - 1:
                    result = (child_descriptor, parent_descriptor, name)
                    parent_descriptor = -1
                    return result
                os.close(parent_descriptor)
                parent_descriptor = child_descriptor
        except OSError:
            self._fail_closed()
            return None
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
        self._fail_closed()
        return None

    def _verify_current_chain(self, snapshots_descriptor: int) -> bool:
        opened = self._open_current_directory(snapshots_descriptor)
        if opened is None:
            return False
        current_descriptor, parent_descriptor, _name = opened
        os.close(current_descriptor)
        os.close(parent_descriptor)
        return True

    def _call_mutation_hook(
        self,
        operation: str,
        components: tuple[str, ...],
        name: str,
    ) -> None:
        hook = self._before_mutation_hook
        if hook is not None:
            hook(operation, components, name)

    def step(self, *, max_entries: int, absolute_deadline: float | None = None) -> int:
        deadline = float("inf") if absolute_deadline is None else absolute_deadline
        consumed = 0
        if self.done or self.failed_closed or max_entries <= 0:
            return consumed
        while (
            not self.done
            and not self.failed_closed
            and consumed < max_entries
            and time.monotonic() < deadline
        ):
            snapshots_descriptor = self._open_snapshots_root()
            if snapshots_descriptor < 0:
                break
            opened = self._open_current_directory(snapshots_descriptor)
            if opened is None:
                os.close(snapshots_descriptor)
                break
            current_descriptor, parent_descriptor, current_name = opened
            frame = self._frames[-1]
            try:
                self._call_mutation_hook("chmod", frame.components, current_name)
                if not self._verify_current_chain(snapshots_descriptor):
                    break
                if time.monotonic() >= deadline:
                    break
                current_details = os.fstat(current_descriptor)
                if (
                    current_details.st_dev != frame.device
                    or current_details.st_ino != frame.inode
                ):
                    self._fail_closed()
                    break
                os.fchmod(current_descriptor, 0o700)
                page = _read_fd_directory_page(
                    current_descriptor,
                    cookie=frame.cookie,
                    max_entries=1,
                    absolute_deadline=deadline,
                )
                if page.entries:
                    entry = page.entries[0]
                    entry_details = os.stat(
                        entry.name,
                        dir_fd=current_descriptor,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(entry_details.st_mode):
                        child_descriptor = os.open(
                            entry.name,
                            self._directory_flags(),
                            dir_fd=current_descriptor,
                        )
                        try:
                            opened_child = os.fstat(child_descriptor)
                            if (
                                opened_child.st_dev != entry_details.st_dev
                                or opened_child.st_ino != entry_details.st_ino
                            ):
                                self._fail_closed()
                                break
                        finally:
                            os.close(child_descriptor)
                        if len(self._frames) >= _ANALYSIS_REAPER_MAX_DEPTH:
                            self._fail_closed()
                            break
                        frame.cookie = page.cookie
                        self._frames.append(
                            _SnapshotDeletionFrame(
                                components=frame.components + (entry.name,),
                                cookie=0,
                                device=entry_details.st_dev,
                                inode=entry_details.st_ino,
                            )
                        )
                        consumed += 1
                        continue
                    self._call_mutation_hook(
                        "unlink",
                        frame.components,
                        entry.name,
                    )
                    if not self._verify_current_chain(snapshots_descriptor):
                        break
                    if time.monotonic() >= deadline:
                        break
                    confirmed = os.stat(
                        entry.name,
                        dir_fd=current_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        confirmed.st_dev != entry_details.st_dev
                        or confirmed.st_ino != entry_details.st_ino
                        or stat.S_IFMT(confirmed.st_mode)
                        != stat.S_IFMT(entry_details.st_mode)
                    ):
                        self._fail_closed()
                        break
                    os.unlink(entry.name, dir_fd=current_descriptor)
                    frame.cookie = page.cookie
                    consumed += 1
                    continue
                if not page.done:
                    break
                self._call_mutation_hook("rmdir", frame.components, current_name)
                if not self._verify_current_chain(snapshots_descriptor):
                    break
                if time.monotonic() >= deadline:
                    break
                confirmed = os.stat(
                    current_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(confirmed.st_mode)
                    or confirmed.st_dev != frame.device
                    or confirmed.st_ino != frame.inode
                ):
                    self._fail_closed()
                    break
                os.rmdir(current_name, dir_fd=parent_descriptor)
                consumed += 1
                if len(self._frames) == 1:
                    self.done = True
                else:
                    self._frames.pop()
            except OSError:
                self._fail_closed()
            finally:
                os.close(current_descriptor)
                os.close(parent_descriptor)
                os.close(snapshots_descriptor)
        return consumed

    def close(self) -> None:
        return None


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
