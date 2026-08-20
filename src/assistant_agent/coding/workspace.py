"""Thread-scoped Git worktrees and governed read operations."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from assistant_agent.coding.config import CodingConfig
from assistant_agent.coding.models import (
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
_MAX_GIT_OUTPUT = 262_144


class CodingWorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message or 'coding workspace operation failed'}")


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
        if start_line < 1 or end_line < start_line or end_line - start_line > 2_000:
            raise CodingWorkspaceError("invalid_tool_input")
        candidate = self._read_path(workspace, path)
        if not candidate.is_file() or candidate.is_symlink():
            raise CodingWorkspaceError("path_invalid")
        if candidate.stat().st_size > self.config.max_file_bytes:
            raise CodingWorkspaceError("file_too_large")
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines(keepends=True)
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
        actual_digest = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
        if actual_digest != proposal.patch_digest:
            raise CodingWorkspaceError("approval_digest_mismatch")
        if proposal.base_commit != workspace.base_commit:
            raise CodingWorkspaceError("base_commit_changed")
        with self._lock(workspace.workspace_ref):
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["CodingWorkspaceError", "CodingWorkspaceService"]
