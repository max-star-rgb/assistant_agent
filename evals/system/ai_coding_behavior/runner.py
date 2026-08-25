"""Operator-gated native CodingGraph behavior system-eval runner."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from time import monotonic
from typing import Callable, Mapping

import httpx
import hmac
from pydantic import ValidationError

from assistant_agent.agent_server.attestation import (
    AgentServerExecutionAttestation,
    coding_registry_digest,
)
from assistant_agent.coding.config import (
    CodingCommandConfig,
    CodingConfig,
    CodingRepositoryConfig,
)
from assistant_agent.coding.models import CodingWorkspace
from assistant_agent.coding.sandbox import DockerCodingSandboxBackend
from assistant_agent.coding.validation import CodingValidationService
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.evaluation.coding_agent_server import (
    CodingBehaviorAgentServerDriver,
    CodingBehaviorDriverResult,
    DriverOutcome,
    FixtureApprovalPolicy,
)
from assistant_agent.evaluation.coding_behavior import (
    SCHEMA_VERSION,
    CodingBehaviorCase,
    CodingBehaviorCaseBinding,
    CodingBehaviorCaseResult,
    CodingBehaviorDryRunReport,
    CodingBehaviorError,
    CodingBehaviorSuite,
    CodingBehaviorSuiteBinding,
    CodingBehaviorSuiteResult,
    build_coding_behavior_dry_run,
    validate_coding_behavior_suite_result,
)
from evals.system.ai_coding_behavior.fixtures import (
    CodingBehaviorFixture,
    CodingBehaviorFixtureStore,
    FixtureCreationError,
    governed_git_environment,
)
from evals.system.ai_coding_behavior.graders import (
    CodingBehaviorGradeInput,
    HeldOutValidationRequest,
    HeldOutValidationResult,
    grade_coding_behavior_case,
)
from evals.system.common.artifacts import create_run_dir, write_json


BASELINE_SUITE_ID = "baseline-v1"
FIXED_SERVER_URL = "http://127.0.0.1:8089"
_REPOSITORY_COMMAND_ID = "coding-eval-validation-v1"
_SANDBOX_IMAGE_PATTERN = (
    "0123456789abcdef"
)
_MAX_ARTIFACT_BYTES = 1_048_576
_MAX_DRIVER_EVIDENCE_BYTES = 16_384
_MAX_MANIFEST_BYTES = 262_144
_MANIFEST_REPOSITORY_PATH = "evals/system/ai_coding_behavior/cases.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = Path(__file__).with_name("cases.json")
_OUTPUT_ROOT = _REPO_ROOT / ".data" / "evals" / "system" / "ai_coding_behavior"
_WORK_PARENT = _OUTPUT_ROOT / "work"


class CodingBehaviorRunnerConfigurationError(RuntimeError):
    """A real evaluation did not satisfy its explicit operator gates."""


@dataclass(frozen=True, slots=True)
class CodingBehaviorRealRunOptions:
    suite_id: str
    server_url: str
    sandbox_image: str
    expected_chat_provider: str
    expected_chat_adapter: str
    expected_model_id: str


@dataclass(frozen=True, slots=True)
class _PreparedCase:
    case: CodingBehaviorCase
    fixture: CodingBehaviorFixture
    repository_id: str
    repository: CodingRepositoryConfig


_HELD_OUT_PROGRAMS: Mapping[str, str] = {
    "single-file-logic-bug-v1": (
        "from src.range_check import contains\n"
        "assert contains(10, 0, 10) is True\n"
        "assert contains(-1, 0, 10) is False\n"
    ),
    "multi-file-interface-v1": (
        "from src.client import render_user\n"
        "assert render_user({'first_name': 'Ada', 'last_name': 'Lovelace'}) == 'Lovelace, Ada'\n"
    ),
    "regression-test-required-v1": (
        "from src.escaping import escape_html\n"
        "assert escape_html('&<>') == '&amp;&lt;&gt;'\n"
    ),
    "scope-discipline-v1": (
        "from src.total import calculate_total\n"
        "assert calculate_total([1, 2, 3]) == 6\n"
        "assert calculate_total([]) == 0\n"
    ),
}


def load_baseline_suite(path: Path = _MANIFEST_PATH) -> CodingBehaviorSuite:
    """Load only the tracked, exact baseline suite."""

    if path != _MANIFEST_PATH:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite must use the tracked manifest"
        )
    parent_descriptor = -1
    descriptor = -1
    try:
        parent = path.parent
        if (
            parent.resolve(strict=True) != parent
            or _REPO_ROOT.resolve(strict=True) != _REPO_ROOT
            or path.relative_to(_REPO_ROOT).as_posix() != _MANIFEST_REPOSITORY_PATH
        ):
            raise CodingBehaviorRunnerConfigurationError(
                "coding behavior suite requires a canonical checkout parent"
            )
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_before = os.fstat(parent_descriptor)
        path_before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_MANIFEST_BYTES
        ):
            raise CodingBehaviorRunnerConfigurationError(
                "coding behavior suite manifest is not a bounded regular file"
            )
        payload_parts: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            payload_parts.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(payload_parts)
        if len(payload) != metadata.st_size:
            raise CodingBehaviorRunnerConfigurationError(
                "coding behavior suite manifest changed while loading"
            )
        _require_manifest_head_blob(payload)
        metadata_after = os.fstat(descriptor)
        path_after = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        parent_after = os.fstat(parent_descriptor)
        if (
            _stat_identity(path_before) != _stat_identity(metadata)
            or _stat_identity(metadata) != _stat_identity(metadata_after)
            or _stat_identity(metadata_after) != _stat_identity(path_after)
            or _directory_identity(parent_before) != _directory_identity(parent_after)
        ):
            raise CodingBehaviorRunnerConfigurationError(
                "coding behavior suite manifest changed while loading"
            )
        suite = CodingBehaviorSuite.model_validate_json(payload)
    except CodingBehaviorRunnerConfigurationError:
        raise
    except (OSError, ValidationError) as exc:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite is invalid"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    if suite.suite_id != BASELINE_SUITE_ID:
        raise CodingBehaviorRunnerConfigurationError(
            f"system eval requires exact suite {BASELINE_SUITE_ID}"
        )
    return suite


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size)


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _manifest_git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _manifest_git(*arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *arguments],
            check=True,
            capture_output=True,
            env=_manifest_git_environment(),
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite Git HEAD authority is unavailable"
        ) from exc


def _require_manifest_head_blob(payload: bytes) -> None:
    head = _manifest_git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if not __import__("re").fullmatch(r"[0-9a-f]{40,64}", head):
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite Git HEAD authority is invalid"
        )
    entry = _manifest_git(
        "ls-tree",
        "--full-tree",
        "-z",
        head,
        "--",
        _MANIFEST_REPOSITORY_PATH,
    )
    prefix = b"100644 blob "
    suffix = b"\t" + _MANIFEST_REPOSITORY_PATH.encode("ascii") + b"\0"
    if not entry.startswith(prefix) or not entry.endswith(suffix) or entry.count(b"\0") != 1:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite is not an exact Git HEAD blob"
        )
    object_id = entry[len(prefix) : -len(suffix)].decode("ascii")
    if not __import__("re").fullmatch(r"[0-9a-f]{40,64}", object_id):
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite Git HEAD blob identity is invalid"
        )
    if _manifest_git("cat-file", "blob", object_id) != payload:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite bytes do not match the exact Git HEAD blob"
        )


def build_real_run_options(
    *,
    suite_id: str,
    server_url: str,
    allow_real_provider: bool,
    allow_local_git_mutation: bool,
    sandbox_image: str | None = None,
    expected_chat_provider: str | None = None,
    expected_chat_adapter: str | None = None,
    expected_model_id: str | None = None,
) -> CodingBehaviorRealRunOptions:
    if not allow_real_provider:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires --allow-real-provider"
        )
    if not allow_local_git_mutation:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires --allow-local-git-mutation"
        )
    if server_url != FIXED_SERVER_URL:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires http://127.0.0.1:8089"
        )
    if suite_id != BASELINE_SUITE_ID:
        raise CodingBehaviorRunnerConfigurationError(
            f"real mode requires exact suite {BASELINE_SUITE_ID}"
        )
    image = (sandbox_image or "").strip()
    prefix, separator, digest = image.rpartition("@sha256:")
    if (
        not separator
        or not prefix
        or len(digest) != 64
        or any(character not in _SANDBOX_IMAGE_PATTERN for character in digest)
    ):
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires a digest-pinned --sandbox-image"
        )
    expected = tuple(
        _safe_external_identifier(value)
        for value in (
            expected_chat_provider,
            expected_chat_adapter,
            expected_model_id,
        )
    )
    return CodingBehaviorRealRunOptions(
        suite_id=suite_id,
        server_url=server_url,
        sandbox_image=image,
        expected_chat_provider=expected[0],
        expected_chat_adapter=expected[1],
        expected_model_id=expected[2],
    )


class IsolatedHeldOutValidationExecutor:
    """Run a fixed held-out command through the existing network-none sandbox."""

    def __init__(self, *, work_root: Path, sandbox_image: str) -> None:
        self._work_root = work_root.resolve()
        self._sandbox_image = sandbox_image
        self._sandbox = DockerCodingSandboxBackend(
            owner_id=f"coding-eval-{secrets.token_hex(8)}"
        )
        self._snapshot_secret = secrets.token_bytes(32)
        self._cleanup_debts: list[_SnapshotCleanupDebt] = []

    def execute(self, request: HeldOutValidationRequest) -> HeldOutValidationResult:
        program = _HELD_OUT_PROGRAMS.get(request.command_id)
        if program is None:
            raise ValueError("held-out command is not in the trusted catalog")
        self._require_repository_binding(request)
        snapshot_parent = self._work_root / "held-out-snapshots"
        _prepare_owned_directory(snapshot_parent)
        debt = _SnapshotCleanupDebt.reserve(snapshot_parent, prefix="snapshot-")
        temporary = debt.path
        snapshot = temporary / "repository"
        snapshot_fd = -1
        cleanup_pending = False
        validation: HeldOutValidationResult | None = None
        try:
            snapshot_binding = _materialize_git_tree_snapshot(
                repository_fd=request.repository_fd,
                expected_commit=request.expected_commit,
                expected_tree=request.expected_tree_digest,
                destination=snapshot,
                secret=self._snapshot_secret,
                max_entries=4_096,
                max_bytes=67_108_864,
            )
            snapshot_fd = os.open(
                snapshot,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            if not _snapshot_binding_matches(
                snapshot_fd,
                snapshot_binding,
                secret=self._snapshot_secret,
                max_entries=4_096,
                max_bytes=67_108_864,
            ):
                raise ValueError("held-out materialized snapshot changed")
            snapshot_path = Path(f"/proc/self/fd/{snapshot_fd}")
            repo_id = f"held-out-{sha256(request.command_id.encode()).hexdigest()[:16]}"
            command = CodingCommandConfig(
                command_id=request.command_id,
                kind="test",
                argv=("python", "-c", program),
                timeout_seconds=request.timeout_seconds,
                cpu_seconds=min(request.timeout_seconds, 120),
                max_output_bytes=65_536,
                max_disk_bytes=67_108_864,
                max_files=4_096,
            )
            repository = CodingRepositoryConfig(
                repo_id=repo_id,
                path=snapshot_path,
                target_branch="main",
                commands={request.command_id: command},
                verification_sequence=(request.command_id,),
                sandbox_enabled=True,
                sandbox_image=self._sandbox_image,
            )
            validation_root = self._work_root / "held-out-validation"
            config = CodingConfig(
                enabled=True,
                workspace_root=validation_root,
                repositories={repo_id: repository},
                max_changed_files=16,
                max_patch_bytes=65_536,
                max_file_bytes=1_048_576,
            )
            result = CodingValidationService(
                CodingWorkspaceService(config),
                sandbox_backend=self._sandbox,
            ).run(
                CodingWorkspace(
                    workspace_ref=f"held-out-{sha256((request.command_id + request.expected_commit).encode()).hexdigest()}",
                    root=snapshot_path,
                    repo_id=repo_id,
                    base_commit=request.expected_commit,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                ),
                repository,
                format_round=0,
            )
            if not _snapshot_binding_matches(
                snapshot_fd,
                snapshot_binding,
                secret=self._snapshot_secret,
                max_entries=4_096,
                max_bytes=67_108_864,
            ):
                raise ValueError("held-out materialized snapshot changed")
            if _git_tree_manifest_digest(
                request.repository_fd,
                expected_commit=request.expected_commit,
                max_entries=4_096,
                max_bytes=67_108_864,
            ) != snapshot_binding.manifest_digest:
                raise ValueError("held-out source Git tree inventory changed")
            self._require_repository_binding(request)
            if len(result.evidence) != 1:
                raise ValueError("held-out validation returned an invalid evidence inventory")
            evidence = result.evidence[0]
            status = evidence.status
            error_category = {
                "passed": "none",
                "timed_out": "timed_out",
                "resource_exceeded": "resource_exceeded",
            }.get(status, "output_limit" if evidence.truncated else "failed")
            if evidence.cleanup_status not in {None, "removed", "not_created"}:
                status = "failed"
                error_category = "cleanup_pending"
                cleanup_pending = True
            validation = HeldOutValidationResult(
                status=status,
                returncode=evidence.exit_code,
                stdout_digest=sha256(evidence.stdout.encode("utf-8")).hexdigest(),
                stderr_digest=sha256(evidence.stderr.encode("utf-8")).hexdigest(),
                error_category=error_category,
            )
        finally:
            if snapshot_fd >= 0:
                os.close(snapshot_fd)
            if not debt.retry():
                self._cleanup_debts.append(debt)
                cleanup_pending = True
        if cleanup_pending:
            empty = sha256(b"").hexdigest()
            return HeldOutValidationResult(
                status="failed",
                returncode=None,
                stdout_digest=validation.stdout_digest if validation else empty,
                stderr_digest=validation.stderr_digest if validation else empty,
                error_category="cleanup_pending",
            )
        if validation is None:
            raise ValueError("held-out validation did not return evidence")
        return validation

    def close(self) -> bool:
        for _ in range(2):
            self._cleanup_debts = [
                debt for debt in self._cleanup_debts if not debt.retry()
            ]
            if not self._cleanup_debts:
                break
        sandbox_released = asyncio.run(self._sandbox.aclose())
        return not self._cleanup_debts and sandbox_released

    @staticmethod
    def _require_repository_binding(request: HeldOutValidationRequest) -> None:
        metadata = os.fstat(request.repository_fd)
        if metadata.st_ino != request.repository_inode:
            raise ValueError("held-out repository descriptor identity changed")
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD", "HEAD^{tree}"),
            cwd=f"/proc/self/fd/{request.repository_fd}",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=governed_git_environment(),
            pass_fds=request.pass_fds,
            check=False,
        )
        lines = completed.stdout.decode("ascii", errors="strict").splitlines()
        if (
            completed.returncode != 0
            or lines != [request.expected_commit, request.expected_tree_digest]
            or len(completed.stderr) > 65_536
        ):
            raise ValueError("held-out repository binding changed")
        status = subprocess.run(
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=f"/proc/self/fd/{request.repository_fd}",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=governed_git_environment(),
            pass_fds=request.pass_fds,
            check=False,
        )
        if (
            status.returncode != 0
            or status.stdout
            or len(status.stderr) > 65_536
        ):
            raise ValueError("held-out repository working tree is not exact and clean")


def _snapshot_copy_hook() -> None:
    """Test seam after source inventory freeze and before descriptor copy."""


@dataclass(frozen=True, slots=True)
class MaterializedGitTreeSnapshot:
    manifest_digest: str
    materialized_digest: str
    manifest_mac: str


@dataclass(slots=True)
class _FixtureCleanupDebt:
    _store: CodingBehaviorFixtureStore
    _fixture: CodingBehaviorFixture
    _case: CodingBehaviorCase
    _released: bool = False

    def retry(self) -> bool:
        if self._released:
            return True
        try:
            self._store.cleanup(self._fixture, self._case)
        except Exception:
            return False
        self._released = True
        return True

    def __reduce__(self) -> object:
        raise TypeError("fixture cleanup debt is not serializable")


class _SnapshotCleanupDebt:
    __slots__ = (
        "_path",
        "_parent_fd",
        "_directory_fd",
        "_device",
        "_inode",
        "_released",
    )

    def __init__(
        self,
        path: Path,
        parent_fd: int,
        directory_fd: int,
        device: int,
        inode: int,
    ) -> None:
        self._path = path
        self._parent_fd = parent_fd
        self._directory_fd = directory_fd
        self._device = device
        self._inode = inode
        self._released = False

    @classmethod
    def reserve(cls, parent: Path, *, prefix: str) -> "_SnapshotCleanupDebt":
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        path: Path | None = None
        try:
            path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
            directory_fd = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except Exception:
            if path is not None:
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            os.close(parent_fd)
            raise
        metadata = os.fstat(directory_fd)
        return cls(
            path,
            parent_fd,
            directory_fd,
            metadata.st_dev,
            metadata.st_ino,
        )

    @property
    def path(self) -> Path:
        return self._path

    def retry(self) -> bool:
        if self._released:
            return True
        try:
            matches = []
            for name in os.listdir(self._parent_fd):
                metadata = os.stat(
                    name,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISDIR(metadata.st_mode)
                    and metadata.st_dev == self._device
                    and metadata.st_ino == self._inode
                ):
                    matches.append(name)
        except OSError:
            return False
        if not matches:
            try:
                released = os.fstat(self._directory_fd).st_nlink == 0
            except OSError:
                released = False
            if released:
                self._release_descriptors()
            return released
        if len(matches) != 1:
            return False
        try:
            _remove_snapshot_fd_tree(self._directory_fd)
            os.rmdir(matches[0], dir_fd=self._parent_fd)
        except OSError:
            return False
        self._release_descriptors()
        return True

    def _release_descriptors(self) -> None:
        if self._released:
            return
        os.close(self._directory_fd)
        os.close(self._parent_fd)
        self._released = True

    def __reduce__(self) -> object:
        raise TypeError("snapshot cleanup debt is not serializable")


def _remove_snapshot_fd_tree(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            try:
                _remove_snapshot_fd_tree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _materialize_git_tree_snapshot(
    *,
    repository_fd: int,
    expected_commit: str,
    expected_tree: str,
    destination: Path,
    secret: bytes,
    max_entries: int,
    max_bytes: int,
) -> MaterializedGitTreeSnapshot:
    if len(secret) < 32:
        raise ValueError("snapshot MAC secret is too short")
    actual = _git_fd_bytes(
        repository_fd,
        ("rev-parse", "HEAD", "HEAD^{tree}"),
        max_output=256,
    ).decode("ascii").splitlines()
    if actual != [expected_commit, expected_tree]:
        raise ValueError("held-out final Git tree binding changed")
    raw_inventory = _git_fd_bytes(
        repository_fd,
        ("ls-tree", "-rz", "--full-tree", "-r", expected_commit),
        max_output=max(65_536, max_entries * 320),
    )
    records = tuple(item for item in raw_inventory.split(b"\0") if item)
    if not records or len(records) > max_entries:
        raise ValueError("held-out Git tree inventory exceeded its bound")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    destination_fd = os.open(
        destination,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    manifest: list[tuple[str, str, str, int, str]] = []
    total_bytes = 0
    try:
        for record in records:
            try:
                header, raw_path = record.split(b"\t", 1)
                raw_mode, raw_type, raw_oid = header.split(b" ", 2)
                mode = raw_mode.decode("ascii")
                object_type = raw_type.decode("ascii")
                object_id = raw_oid.decode("ascii")
                path = raw_path.decode("utf-8")
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("held-out Git tree inventory is invalid") from exc
            if (
                object_type != "blob"
                or mode not in {"100644", "100755"}
                or len(object_id) not in {40, 64}
                or any(character not in "0123456789abcdef" for character in object_id)
            ):
                raise ValueError("held-out Git tree contains an unsupported object")
            _canonical_snapshot_path(path)
            size_raw = _git_fd_bytes(
                repository_fd,
                ("cat-file", "-s", object_id),
                max_output=32,
            ).decode("ascii").strip()
            if not size_raw.isdigit():
                raise ValueError("held-out Git blob size is invalid")
            size = int(size_raw)
            total_bytes += size
            if total_bytes > max_bytes:
                raise ValueError("held-out Git tree byte budget exceeded")
            content = _git_fd_bytes(
                repository_fd,
                ("cat-file", "blob", object_id),
                max_output=size,
            )
            if len(content) != size:
                raise ValueError("held-out Git blob size changed")
            _write_snapshot_blob(
                destination_fd,
                path,
                content,
                executable=mode == "100755",
            )
            manifest.append((path, mode, object_id, size, sha256(content).hexdigest()))
        materialized_digest = _logical_fd_digest(
            destination_fd, max_entries, max_bytes
        )
    finally:
        os.close(destination_fd)
    manifest_digest = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_mac = hmac.new(
        secret,
        f"coding-held-out-snapshot-v1\0{manifest_digest}\0{materialized_digest}".encode(
            "ascii"
        ),
        sha256,
    ).hexdigest()
    return MaterializedGitTreeSnapshot(
        manifest_digest=manifest_digest,
        materialized_digest=materialized_digest,
        manifest_mac=manifest_mac,
    )


def _snapshot_binding_matches(
    snapshot_fd: int,
    binding: MaterializedGitTreeSnapshot,
    *,
    secret: bytes,
    max_entries: int,
    max_bytes: int,
) -> bool:
    materialized = _logical_fd_digest(snapshot_fd, max_entries, max_bytes)
    expected_mac = hmac.new(
        secret,
        f"coding-held-out-snapshot-v1\0{binding.manifest_digest}\0{materialized}".encode(
            "ascii"
        ),
        sha256,
    ).hexdigest()
    return hmac.compare_digest(materialized, binding.materialized_digest) and hmac.compare_digest(
        expected_mac, binding.manifest_mac
    )


def _git_tree_manifest_digest(
    repository_fd: int,
    *,
    expected_commit: str,
    max_entries: int,
    max_bytes: int,
) -> str:
    raw_inventory = _git_fd_bytes(
        repository_fd,
        ("ls-tree", "-rz", "--full-tree", "-r", expected_commit),
        max_output=max(65_536, max_entries * 320),
    )
    records = tuple(item for item in raw_inventory.split(b"\0") if item)
    if not records or len(records) > max_entries:
        raise ValueError("held-out Git tree inventory exceeded its bound")
    manifest: list[tuple[str, str, str, int, str]] = []
    total_bytes = 0
    for record in records:
        try:
            header, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_oid = header.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_oid.decode("ascii")
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("held-out Git tree inventory is invalid") from exc
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("held-out Git tree contains an unsupported object")
        _canonical_snapshot_path(path)
        content = _git_fd_bytes(
            repository_fd,
            ("cat-file", "blob", object_id),
            max_output=max_bytes - total_bytes,
        )
        total_bytes += len(content)
        if total_bytes > max_bytes:
            raise ValueError("held-out Git tree byte budget exceeded")
        manifest.append((path, mode, object_id, len(content), sha256(content).hexdigest()))
    return sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _artifact_identifier_digest(domain: bytes, value: str) -> str:
    return sha256(domain + b"\0" + value.encode("utf-8")).hexdigest()


def _artifact_attestation_projection(
    attestation: AgentServerExecutionAttestation,
) -> dict[str, object]:
    return {
        "schema_version": attestation.schema_version,
        "graph_id": attestation.graph_id,
        "provider_mode": attestation.provider_mode,
        "chat_provider": attestation.chat_provider,
        "chat_adapter": attestation.chat_adapter,
        "model_id": attestation.model_id,
        "coding_enabled": attestation.coding_enabled,
        "coding_registry_digest": attestation.coding_registry_digest,
        "process_boot_digest": _artifact_identifier_digest(
            b"ai-coding-eval-boot-v1", attestation.process_boot_nonce
        ),
        "repository_binding_digests": sorted(
            _artifact_identifier_digest(
                b"ai-coding-eval-repository-v1",
                f"{repository_id}\0{config_digest}",
            )
            for repository_id, config_digest in attestation.repository_config_digests.items()
        ),
        "attestation_digest": attestation.canonical_digest(),
    }


def _git_fd_bytes(
    repository_fd: int, arguments: tuple[str, ...], *, max_output: int
) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=f"/proc/self/fd/{repository_fd}",
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        env=governed_git_environment(),
        pass_fds=(repository_fd,),
        check=False,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > max_output
        or len(completed.stderr) > 65_536
    ):
        raise ValueError("held-out Git object materialization failed")
    return completed.stdout


def _canonical_snapshot_path(value: str) -> tuple[str, ...]:
    parts = tuple(value.split("/"))
    if (
        not parts
        or value.startswith("/")
        or any(
            not part
            or part in {".", "..", ".git"}
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in part
            )
            for part in parts
        )
    ):
        raise ValueError("held-out Git tree path is noncanonical")
    return parts


def _write_snapshot_blob(
    root_fd: int,
    path: str,
    content: bytes,
    *,
    executable: bool,
) -> None:
    parts = _canonical_snapshot_path(path)
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o755 if executable else 0o644,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(content)
            while view:
                count = os.write(file_fd, view)
                view = view[count:]
            os.fchmod(file_fd, 0o755 if executable else 0o644)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _materialize_fd_snapshot(
    *,
    source_fd: int,
    destination: Path,
    max_entries: int,
    max_bytes: int,
) -> str:
    before_identity, before_logical = _fd_inventory(
        source_fd, max_entries=max_entries, max_bytes=max_bytes
    )
    _snapshot_copy_hook()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    destination_fd = os.open(
        destination,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        budget = {"entries": 0, "bytes": 0}
        _copy_fd_tree(
            source_fd,
            destination_fd,
            max_entries=max_entries,
            max_bytes=max_bytes,
            budget=budget,
        )
        after_identity, after_logical = _fd_inventory(
            source_fd, max_entries=max_entries, max_bytes=max_bytes
        )
        _, materialized_logical = _fd_inventory(
            destination_fd, max_entries=max_entries, max_bytes=max_bytes
        )
    finally:
        os.close(destination_fd)
    if (
        before_identity != after_identity
        or before_logical != after_logical
        or before_logical != materialized_logical
    ):
        raise ValueError("held-out source or materialized snapshot changed")
    return before_logical


def _logical_fd_digest(directory_fd: int, max_entries: int, max_bytes: int) -> str:
    return _fd_inventory(
        directory_fd, max_entries=max_entries, max_bytes=max_bytes
    )[1]


def _fd_inventory(
    directory_fd: int,
    *,
    max_entries: int,
    max_bytes: int,
) -> tuple[str, str]:
    budget = {"entries": 0, "bytes": 0}
    identity: list[tuple[object, ...]] = []
    logical: list[tuple[object, ...]] = []

    def visit(current_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(current_fd)):
            if name == ".git":
                continue
            if (
                not name
                or name in {".", ".."}
                or any(
                    character
                    not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
                    for character in name
                )
            ):
                raise ValueError("held-out snapshot path is noncanonical")
            budget["entries"] += 1
            if budget["entries"] > max_entries:
                raise ValueError("held-out snapshot entry budget exceeded")
            metadata = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(metadata.st_mode):
                identity.append(
                    (relative, "directory", metadata.st_dev, metadata.st_ino, metadata.st_mode)
                )
                logical.append((relative, "directory"))
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current_fd,
                )
                try:
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("held-out snapshot requires regular single-link files")
            budget["bytes"] += metadata.st_size
            if budget["bytes"] > max_bytes:
                raise ValueError("held-out snapshot byte budget exceeded")
            file_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current_fd,
            )
            digest = sha256()
            try:
                opened = os.fstat(file_fd)
                if (
                    opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                    or opened.st_size != metadata.st_size
                ):
                    raise ValueError("held-out snapshot file identity changed")
                remaining = metadata.st_size
                while remaining:
                    chunk = os.read(file_fd, min(65_536, remaining))
                    if not chunk:
                        raise ValueError("held-out snapshot file was truncated")
                    digest.update(chunk)
                    remaining -= len(chunk)
                closed_over = os.fstat(file_fd)
                if (
                    closed_over.st_dev != opened.st_dev
                    or closed_over.st_ino != opened.st_ino
                    or closed_over.st_size != opened.st_size
                    or closed_over.st_mtime_ns != opened.st_mtime_ns
                ):
                    raise ValueError("held-out snapshot file changed during read")
            finally:
                os.close(file_fd)
            mode = stat.S_IMODE(metadata.st_mode)
            content_digest = digest.hexdigest()
            identity.append(
                (
                    relative,
                    "regular",
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    content_digest,
                )
            )
            logical.append((relative, "regular", mode, metadata.st_size, content_digest))

    visit(directory_fd, "")
    encode = lambda value: json.dumps(  # noqa: E731 - local canonical encoder.
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encode(identity)).hexdigest(), sha256(encode(logical)).hexdigest()


def _copy_fd_tree(
    source_fd: int,
    destination_fd: int,
    *,
    max_entries: int,
    max_bytes: int,
    budget: dict[str, int],
) -> None:
    for name in sorted(os.listdir(source_fd)):
        if name == ".git":
            continue
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        budget["entries"] += 1
        if budget["entries"] > max_entries:
            raise ValueError("held-out snapshot entry budget exceeded")
        if stat.S_ISDIR(metadata.st_mode):
            os.mkdir(name, mode=0o700, dir_fd=destination_fd)
            source_child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_fd,
            )
            destination_child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=destination_fd,
            )
            try:
                _copy_fd_tree(
                    source_child,
                    destination_child,
                    max_entries=max_entries,
                    max_bytes=max_bytes,
                    budget=budget,
                )
            finally:
                os.close(destination_child)
                os.close(source_child)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("held-out snapshot requires regular single-link files")
        budget["bytes"] += metadata.st_size
        if budget["bytes"] > max_bytes:
            raise ValueError("held-out snapshot byte budget exceeded")
        source_file = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=source_fd,
        )
        destination_file = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            stat.S_IMODE(metadata.st_mode),
            dir_fd=destination_fd,
        )
        try:
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(source_file, min(65_536, remaining))
                if not chunk:
                    raise ValueError("held-out snapshot file was truncated")
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_file, view)
                    view = view[written:]
                remaining -= len(chunk)
            os.fchmod(destination_file, stat.S_IMODE(metadata.st_mode))
            os.fsync(destination_file)
        finally:
            os.close(destination_file)
            os.close(source_file)


def run_coding_behavior_eval(
    *,
    real: bool = False,
    suite_id: str | None = None,
    server_url: str = FIXED_SERVER_URL,
    allow_real_provider: bool = False,
    allow_local_git_mutation: bool = False,
    sandbox_image: str | None = None,
    expected_chat_provider: str | None = None,
    expected_chat_adapter: str | None = None,
    expected_model_id: str | None = None,
    _test_confirmation_capability: object | None = None,
) -> CodingBehaviorDryRunReport | CodingBehaviorSuiteResult:
    suite = load_baseline_suite()
    if not real:
        if suite_id not in {None, suite.suite_id}:
            raise CodingBehaviorRunnerConfigurationError(
                f"dry-run requires exact suite {BASELINE_SUITE_ID}"
            )
        return build_coding_behavior_dry_run(suite)

    options = build_real_run_options(
        suite_id=suite_id or "",
        server_url=server_url,
        allow_real_provider=allow_real_provider,
        allow_local_git_mutation=allow_local_git_mutation,
        sandbox_image=sandbox_image,
        expected_chat_provider=expected_chat_provider,
        expected_chat_adapter=expected_chat_adapter,
        expected_model_id=expected_model_id,
    )
    return _run_real_suite(
        suite,
        options=options,
        test_confirmation_capability=_test_confirmation_capability,
    )


def _run_real_suite(
    suite: CodingBehaviorSuite,
    *,
    options: CodingBehaviorRealRunOptions,
    test_confirmation_capability: object | None,
) -> CodingBehaviorSuiteResult:
    started = monotonic()
    _prepare_owned_directory(_OUTPUT_ROOT)
    _prepare_owned_directory(_WORK_PARENT)
    store = CodingBehaviorFixtureStore(_WORK_PARENT)
    prepared: list[_PreparedCase] = []
    case_results: dict[str, CodingBehaviorCaseResult] = {}
    fixture_cleanup_debts: list[tuple[str, _FixtureCleanupDebt]] = []
    thread_cleanup_debts: list[tuple[str, object]] = []
    outer_cleanup_pending: set[str] = set()
    attestation: AgentServerExecutionAttestation | None = None
    identity = f"coding-eval-{secrets.token_hex(12)}"
    executor = IsolatedHeldOutValidationExecutor(
        work_root=_WORK_PARENT,
        sandbox_image=options.sandbox_image,
    )
    try:
        for case in suite.cases:
            fixture = store.create(case)
            fixture_cleanup_debts.append(
                (case.case_id, _FixtureCleanupDebt(store, fixture, case))
            )
            repository_id = f"eval-{sha256((identity + case.case_id).encode()).hexdigest()[:24]}"
            repository = _server_repository(
                fixture,
                repository_id=repository_id,
                sandbox_image=options.sandbox_image,
            )
            prepared.append(_PreparedCase(case, fixture, repository_id, repository))
        binding = _binding_projection(prepared, identity=identity)
        confirmation_nonce = secrets.token_hex(16)
        try:
            attestation = _confirm_server_binding(
                binding,
                prepared=prepared,
                identity=identity,
                options=options,
                confirmation_nonce=confirmation_nonce,
                test_capability=test_confirmation_capability,
            )
        except (httpx.TransportError, httpx.TimeoutException):
            binding_failure_category = "transport"
        except Exception:
            binding_failure_category = "configuration"
        else:
            binding_failure_category = None
        run_items = prepared if attestation is not None else []
        if attestation is None:
            for item in prepared:
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    (
                        "coding_eval_server_unavailable"
                        if binding_failure_category == "transport"
                        else "coding_eval_configuration_error"
                    ),
                    "Server execution attestation was not confirmed.",
                    failure_category=binding_failure_category or "configuration",
                )
        for item in run_items:
            outcome: DriverOutcome | None = None
            cleanup_pending = False
            try:
                expected_attestation = attestation
                attestation = None
                pre_attestation = _sample_server_attestation(
                    options.server_url,
                    identity,
                    test_confirmation_capability,
                )
                _require_expected_attestation(
                    pre_attestation, prepared=prepared, options=options
                )
                if pre_attestation != expected_attestation:
                    raise CodingBehaviorRunnerConfigurationError(
                        "server execution attestation changed before case"
                    )
                attestation = pre_attestation
                policy = FixtureApprovalPolicy(
                    store=store,
                    case=item.case,
                    fixture=item.fixture,
                    repository_id=item.repository_id,
                    identity=identity,
                    target_branch="main",
                )
                outcome = CodingBehaviorAgentServerDriver(
                    server_url=options.server_url,
                    identity=identity,
                    max_interrupts=len(item.case.required_interrupts),
                    expected_execution_attestation_digest=(
                        pre_attestation.canonical_digest()
                    ),
                ).run(case=item.case, policy=policy)
                attestation = None
                post_attestation = _sample_server_attestation(
                    options.server_url,
                    identity,
                    test_confirmation_capability,
                )
                _require_expected_attestation(
                    post_attestation, prepared=prepared, options=options
                )
                if post_attestation != pre_attestation:
                    raise CodingBehaviorRunnerConfigurationError(
                        "server execution attestation changed during case"
                    )
                attestation = post_attestation
                case_results[item.case.case_id] = _case_result(
                    item.case,
                    item.fixture,
                    outcome.result,
                    store=store,
                    executor=executor,
                )
            except (httpx.TransportError, ConnectionError, TimeoutError):
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    "coding_eval_server_unavailable",
                    "Evaluation transport failed.",
                    failure_category="transport",
                )
            except PermissionError:
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    "coding_eval_repository_not_bound",
                    "Evaluation permission binding failed.",
                    failure_category="permission",
                )
            except CodingBehaviorRunnerConfigurationError:
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    "coding_eval_configuration_error",
                    "Evaluation execution attestation changed.",
                    failure_category="configuration",
                )
            except Exception:
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    "coding_eval_configuration_error",
                    "Evaluation orchestration failed.",
                    failure_category="internal",
                )
            finally:
                if outcome is not None and outcome.cleanup_debt is not None:
                    cleanup_pending = not outcome.cleanup_debt.retry()
                    if cleanup_pending:
                        thread_cleanup_debts.append(
                            (item.case.case_id, outcome.cleanup_debt)
                        )
                fixture_debt = next(
                    debt
                    for case_id, debt in fixture_cleanup_debts
                    if case_id == item.case.case_id
                )
                cleanup_pending = not fixture_debt.retry() or cleanup_pending
                if cleanup_pending:
                    case_results[item.case.case_id] = _failed_case(
                        item.case,
                        "coding_eval_cleanup_pending",
                        "Evaluation cleanup remains pending.",
                        cleanup_pending=True,
                        prior=case_results.get(item.case.case_id),
                        failure_category="cleanup",
                    )
    except FixtureCreationError as exc:
        matching = next(
            (case for case in suite.cases if case.case_id == exc.fixture.case_id),
            None,
        )
        fixture_cleanup_pending = False
        if matching is not None:
            debt = _FixtureCleanupDebt(store, exc.fixture, matching)
            fixture_cleanup_debts.append((matching.case_id, debt))
            fixture_cleanup_pending = not debt.retry()
            case_results[matching.case_id] = _failed_case(
                matching,
                (
                    "coding_eval_cleanup_pending"
                    if fixture_cleanup_pending
                    else "coding_eval_configuration_error"
                ),
                "Fixture creation failed.",
                cleanup_pending=fixture_cleanup_pending,
                failure_category=(
                    "cleanup" if fixture_cleanup_pending else "configuration"
                ),
            )
            if fixture_cleanup_pending:
                outer_cleanup_pending.add(matching.case_id)
    finally:
        for _ in range(2):
            fixture_cleanup_debts = [
                (case_id, debt)
                for case_id, debt in fixture_cleanup_debts
                if not debt.retry()
            ]
            if not fixture_cleanup_debts:
                break
        outer_cleanup_pending.update(case_id for case_id, _ in fixture_cleanup_debts)
        for case_id, debt in thread_cleanup_debts:
            released = False
            for _ in range(2):
                try:
                    released = debt.retry()
                except Exception:
                    released = False
                if released:
                    break
            if not released:
                outer_cleanup_pending.add(case_id)
        if not executor.close():
            outer_cleanup_pending.update(case.case_id for case in suite.cases)

    for case in suite.cases:
        if case.case_id in outer_cleanup_pending:
            case_results[case.case_id] = _failed_case(
                case,
                "coding_eval_cleanup_pending",
                "Evaluation cleanup remains pending.",
                cleanup_pending=True,
                prior=case_results.get(case.case_id),
                failure_category="cleanup",
            )

    for case in suite.cases:
        case_results.setdefault(
            case.case_id,
            _failed_case(
                case,
                "coding_eval_configuration_error",
                "Evaluation did not execute the complete suite.",
                failure_category="configuration",
            ),
        )
    ordered = tuple(case_results[case.case_id] for case in suite.cases)
    result = CodingBehaviorSuiteResult(
        schema_version=SCHEMA_VERSION,
        suite_id=suite.suite_id,
        execution_profile=suite.execution_profile,
        suite_binding=CodingBehaviorSuiteBinding.from_suite(suite),
        status="passed" if all(item.status == "passed" for item in ordered) else "failed",
        cases=ordered,
        elapsed_ms=min(115_200_000, max(0, int((monotonic() - started) * 1000))),
    )
    validated = validate_coding_behavior_suite_result(suite, result)
    write_result_artifact(
        root=_OUTPUT_ROOT,
        suite=suite,
        result=validated,
        attestation=attestation,
    )
    return validated


def _case_result(
    case: CodingBehaviorCase,
    fixture: CodingBehaviorFixture,
    driver: CodingBehaviorDriverResult,
    *,
    store: CodingBehaviorFixtureStore,
    executor: IsolatedHeldOutValidationExecutor,
) -> CodingBehaviorCaseResult:
    if driver.status != "completed":
        return _failed_case(
            case,
            driver.error_code or "coding_eval_terminal_mismatch",
            "Native coding run failed.",
            elapsed_ms=driver.elapsed_ms,
            cleanup_pending=driver.cleanup_pending,
            failure_category=driver.failure_category,
        )
    required = (
        driver.final_commit,
        driver.validation_tree_digest,
        driver.review_tree_digest,
        driver.integration_tree_digest,
    )
    if any(value is None for value in required):
        return _failed_case(
            case,
            "coding_eval_terminal_mismatch",
            "Native terminal evidence is incomplete.",
            elapsed_ms=driver.elapsed_ms,
            failure_category="terminal",
        )
    evidence_size = len(
        json.dumps(
            [
                {
                    "sequence": item.sequence,
                    "kind": item.kind,
                    "checkpoint_digest": item.checkpoint_digest,
                }
                for item in driver.transitions
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    )
    grade = grade_coding_behavior_case(
        case,
        CodingBehaviorGradeInput(
            fixture=fixture,
            terminal_status=driver.terminal_status or "",
            interrupt_kinds=driver.interrupt_kinds,
            elapsed_ms=driver.elapsed_ms,
            interrupt_count=driver.interrupt_count,
            max_interrupts=len(case.required_interrupts),
            evidence_size_bytes=evidence_size,
            max_evidence_size_bytes=_MAX_DRIVER_EVIDENCE_BYTES,
            validation_tree_digest=driver.validation_tree_digest or "",
            review_tree_digest=driver.review_tree_digest or "",
            integration_tree_digest=driver.integration_tree_digest or "",
            final_commit=driver.final_commit or "",
        ),
        store=store,
        validation_executor=executor,
    )
    cleanup_failed = (
        grade.command_evidence is not None
        and grade.command_evidence.error_category == "cleanup_pending"
    )
    return CodingBehaviorCaseResult(
        schema_version=SCHEMA_VERSION,
        case_id=case.case_id,
        fixture_id=case.fixture_id,
        case_binding=CodingBehaviorCaseBinding.from_case(case),
        status="failed" if cleanup_failed else grade.status,
        checks=grade.checks,
        error=(
            None
            if grade.status == "passed" and not cleanup_failed
            else _error(
                "coding_eval_cleanup_pending" if cleanup_failed else "coding_eval_grader_failed",
                "Evaluation cleanup remains pending."
                if cleanup_failed
                else "A deterministic grader failed.",
            )
        ),
        terminal_status=driver.terminal_status,
        changed_paths=grade.changed_paths,
        elapsed_ms=driver.elapsed_ms,
        cleanup_pending=cleanup_failed,
        failure_category=(
            "cleanup" if cleanup_failed else (None if grade.status == "passed" else "grader")
        ),
    )


def _failed_case(
    case: CodingBehaviorCase,
    code: str,
    message: str,
    *,
    elapsed_ms: int = 0,
    cleanup_pending: bool = False,
    prior: CodingBehaviorCaseResult | None = None,
    failure_category: str = "internal",
) -> CodingBehaviorCaseResult:
    return CodingBehaviorCaseResult(
        schema_version=SCHEMA_VERSION,
        case_id=case.case_id,
        fixture_id=case.fixture_id,
        case_binding=CodingBehaviorCaseBinding.from_case(case),
        status="failed",
        checks=prior.checks if prior is not None else (),
        error=_error(code, message),
        terminal_status=prior.terminal_status if prior is not None else None,
        changed_paths=prior.changed_paths if prior is not None else (),
        elapsed_ms=prior.elapsed_ms if prior is not None else elapsed_ms,
        cleanup_pending=cleanup_pending,
        failure_category=failure_category,  # type: ignore[arg-type]
    )


def _error(code: str, message: str) -> CodingBehaviorError:
    return CodingBehaviorError(
        schema_version=SCHEMA_VERSION,
        code=code,  # type: ignore[arg-type]
        message=message,
    )


def _server_repository(
    fixture: CodingBehaviorFixture,
    *,
    repository_id: str,
    sandbox_image: str,
) -> CodingRepositoryConfig:
    command = CodingCommandConfig(
        command_id=_REPOSITORY_COMMAND_ID,
        kind="test",
        argv=("python", "-m", "compileall", "-q", "src", "tests"),
        timeout_seconds=60,
        cpu_seconds=60,
        max_output_bytes=65_536,
        max_disk_bytes=67_108_864,
        max_files=4_096,
    )
    return CodingRepositoryConfig(
        repo_id=repository_id,
        path=fixture.repository.resolve(strict=True),
        target_branch="main",
        parallel_analysis_enabled=False,
        code_review_enabled=True,
        commands={_REPOSITORY_COMMAND_ID: command},
        verification_sequence=(_REPOSITORY_COMMAND_ID,),
        integration_enabled=True,
        sandbox_enabled=True,
        sandbox_image=sandbox_image,
        dependency_profile=None,
        artifact_profile=None,
        commit_author_name="Assistant Agent Eval",
        commit_author_email="eval@invalid.local",
    )


def _binding_projection(
    prepared: list[_PreparedCase], *, identity: str
) -> dict[str, object]:
    repositories: dict[str, object] = {}
    for item in prepared:
        payload = item.repository.model_dump(mode="json")
        payload.pop("repo_id", None)
        repositories[item.repository_id] = payload
    workspace_root = str((_WORK_PARENT / "server-workspaces").resolve())
    return {
        "schema_version": SCHEMA_VERSION,
        "server": FIXED_SERVER_URL,
        "identity": identity,
        "operator_action": "restart the existing 8089 server with exactly this temporary coding binding",
        "environment": {
            "MULTIMODAL_AGENT_CODING_ENABLED": "true",
            "MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT": workspace_root,
            "MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON": json.dumps(
                repositories, sort_keys=True, separators=(",", ":")
            ),
        },
    }


_TEST_CONFIRMATION_ISSUER = object()


class _TestConfirmationCapability:
    __slots__ = ("attestation", "_issuer", "_consumed")

    def __init__(
        self, attestation: AgentServerExecutionAttestation, *, issuer: object
    ) -> None:
        if issuer is not _TEST_CONFIRMATION_ISSUER:
            raise TypeError("test confirmation capability is store-issued only")
        self.attestation = attestation
        self._issuer = issuer
        self._consumed = False

    def consume(self) -> AgentServerExecutionAttestation:
        if self._issuer is not _TEST_CONFIRMATION_ISSUER or self._consumed:
            raise ValueError("test confirmation capability is invalid or consumed")
        self._consumed = True
        return self.attestation

    def sample(self) -> AgentServerExecutionAttestation:
        if self._issuer is not _TEST_CONFIRMATION_ISSUER or not self._consumed:
            raise ValueError("test confirmation capability is not active")
        return self.attestation


def _issue_test_confirmation_capability(
    attestation: AgentServerExecutionAttestation,
) -> _TestConfirmationCapability:
    return _TestConfirmationCapability(attestation, issuer=_TEST_CONFIRMATION_ISSUER)


def _confirm_server_binding(
    binding: dict[str, object],
    *,
    prepared: list[_PreparedCase],
    identity: str,
    options: CodingBehaviorRealRunOptions,
    confirmation_nonce: str,
    test_capability: object | None,
) -> AgentServerExecutionAttestation:
    if test_capability is None:
        _terminal_reload_confirmation(binding, confirmation_nonce)
        attestation = _fetch_server_attestation(options.server_url, identity)
    elif isinstance(test_capability, _TestConfirmationCapability):
        attestation = test_capability.consume()
    else:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode rejects untrusted confirmation injection"
        )
    _require_expected_attestation(attestation, prepared=prepared, options=options)
    if test_capability is None:
        expected = _operator_ack_value(confirmation_nonce, attestation)
        print(attestation.model_dump_json(indent=2), file=sys.stderr)
        if input(f"Type {expected}: ") != expected:
            raise CodingBehaviorRunnerConfigurationError(
                "operator attestation acknowledgement did not match"
            )
    return attestation


def _sample_server_attestation(
    server_url: str,
    identity: str,
    test_capability: object | None,
) -> AgentServerExecutionAttestation:
    if test_capability is None:
        return _fetch_server_attestation(server_url, identity)
    if isinstance(test_capability, _TestConfirmationCapability):
        return test_capability.sample()
    raise CodingBehaviorRunnerConfigurationError(
        "real mode rejects untrusted attestation sampling"
    )


def _terminal_reload_confirmation(
    binding: dict[str, object], confirmation_nonce: str
) -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires an interactive operator for the 8089 binding reload"
        )
    print(
        json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True),
        file=sys.stderr,
    )
    expected = f"RELOADED {confirmation_nonce}"
    if input(f"Restart the existing 8089 server, then type {expected}: ") != expected:
        raise CodingBehaviorRunnerConfigurationError(
            "operator did not confirm the exact 8089 reload nonce"
        )


def _fetch_server_attestation(
    server_url: str, identity: str
) -> AgentServerExecutionAttestation:
    response = httpx.get(
        f"{server_url}/internal/evaluation/coding-attestation",
        headers={"x-assistant-user": identity},
        timeout=10.0,
        trust_env=False,
    )
    response.raise_for_status()
    if len(response.content) > 16_384:
        raise CodingBehaviorRunnerConfigurationError(
            "server attestation exceeded its input bound"
        )
    return AgentServerExecutionAttestation.model_validate_json(response.content)


def _require_expected_attestation(
    attestation: AgentServerExecutionAttestation,
    *,
    prepared: list[_PreparedCase],
    options: CodingBehaviorRealRunOptions,
) -> None:
    repositories = {item.repository_id: item.repository for item in prepared}
    registry_digest, repository_digests = coding_registry_digest(repositories)
    if (
        attestation.graph_id != "assistant-native-v3"
        or attestation.provider_mode != "real"
        or attestation.chat_provider != options.expected_chat_provider
        or attestation.chat_adapter != options.expected_chat_adapter
        or attestation.model_id != options.expected_model_id
        or not attestation.coding_enabled
        or attestation.coding_registry_digest != registry_digest
        or attestation.repository_config_digests != repository_digests
    ):
        raise CodingBehaviorRunnerConfigurationError(
            "server execution attestation does not match the exact operator expectation"
        )


def _operator_ack_value(
    confirmation_nonce: str,
    attestation: AgentServerExecutionAttestation,
) -> str:
    if len(confirmation_nonce) != 32 or any(
        character not in "0123456789abcdef" for character in confirmation_nonce
    ):
        raise ValueError("confirmation nonce is invalid")
    return (
        f"ACK {confirmation_nonce} {attestation.process_boot_nonce} "
        f"{attestation.coding_registry_digest}"
    )


def _safe_external_identifier(value: str | None) -> str:
    raw = (value or "").strip()
    try:
        probe = AgentServerExecutionAttestation(
            schema_version=1,
            graph_id="assistant-native-v3",
            provider_mode="real",
            chat_provider=raw,
            chat_adapter=raw,
            model_id=raw,
            coding_enabled=True,
            coding_registry_digest="0" * 64,
            repository_config_digests={},
            process_boot_nonce="0" * 32,
        )
    except ValidationError as exc:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires safe exact expected Provider/adapter/model identifiers"
        ) from exc
    return probe.model_id


def write_result_artifact(
    *,
    root: Path,
    suite: CodingBehaviorSuite,
    result: CodingBehaviorSuiteResult,
    attestation: AgentServerExecutionAttestation | None,
) -> Path:
    validated = validate_coding_behavior_suite_result(suite, result)
    failure_categories = {
        case.case_id: case.failure_category
        for case in validated.cases
        if case.status == "failed"
    }
    if any(category is None for category in failure_categories.values()):
        raise CodingBehaviorRunnerConfigurationError(
            "validated failed cases require failure categories"
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "server_attestation": (
            _artifact_attestation_projection(attestation)
            if attestation is not None
            else None
        ),
        "failure_categories": dict(sorted(failure_categories.items())),
        "result": validated.model_dump(mode="json"),
    }
    _require_redacted_artifact_value(payload)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior artifact exceeds its bound"
        )
    _prepare_owned_directory(root)
    run_dir = create_run_dir(root, domain="run", case_id=suite.suite_id)
    temporary = run_dir / ".result.json.tmp"
    destination = run_dir / "result.json"
    write_json(temporary, payload)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return destination


def _require_redacted_artifact_value(value: object, *, key: str = "") -> None:
    forbidden_keys = {
        "messages",
        "patch",
        "prompt",
        "raw_response",
        "repository_path",
        "source",
        "stderr",
        "stdout",
        "thread_id",
    }
    if isinstance(value, dict):
        for child_key, child in value.items():
            if (
                not isinstance(child_key, str)
                or child_key in forbidden_keys
                or _artifact_key_is_secret_like(child_key)
            ):
                raise CodingBehaviorRunnerConfigurationError(
                    "artifact contains a forbidden evidence field"
                )
            _require_redacted_artifact_value(child, key=child_key)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _require_redacted_artifact_value(child, key=key)
        return
    if isinstance(value, str):
        if (
            len(value) > 2_000
            or len(value.encode("utf-8")) > 8_000
            or value != __import__("unicodedata").normalize("NFC", value)
            or any(
                __import__("unicodedata").category(character) in {"Cc", "Cf"}
                for character in value
            )
            or value.startswith(("/", "~", "file://"))
            or any(
                marker in value.lower()
                for marker in ("api_key=", "authorization:", "bearer ", "sk-")
            )
        ):
            raise CodingBehaviorRunnerConfigurationError(
                f"artifact string is unsafe for field {key or '<root>'}"
            )


def _artifact_key_is_secret_like(value: str) -> bool:
    unicodedata = __import__("unicodedata")
    if (
        not value
        or len(value) > 128
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        return True
    compact = "".join(character for character in value.casefold() if character.isalnum())
    return any(
        marker in compact
        for marker in (
            "apikey",
            "accesstoken",
            "refreshtoken",
            "authorization",
            "password",
            "passwd",
            "secret",
            "credential",
            "privatekey",
            "sessioncookie",
        )
    )


def _prepare_owned_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior output root is unsafe"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the native AI coding behavior system evaluation."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="real", action="store_false")
    mode.add_argument("--real", dest="real", action="store_true")
    parser.set_defaults(real=False)
    parser.add_argument("--suite-id")
    parser.add_argument("--server", default=FIXED_SERVER_URL)
    parser.add_argument("--sandbox-image")
    parser.add_argument("--expected-chat-provider")
    parser.add_argument("--expected-chat-adapter")
    parser.add_argument("--expected-model-id")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--allow-local-git-mutation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = run_coding_behavior_eval(
            real=arguments.real,
            suite_id=arguments.suite_id,
            server_url=arguments.server,
            sandbox_image=arguments.sandbox_image,
            allow_real_provider=arguments.allow_real_provider,
            allow_local_git_mutation=arguments.allow_local_git_mutation,
            expected_chat_provider=arguments.expected_chat_provider,
            expected_chat_adapter=arguments.expected_chat_adapter,
            expected_model_id=arguments.expected_model_id,
        )
    except CodingBehaviorRunnerConfigurationError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "failed",
                    "error_code": "coding_eval_configuration_error",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(report.model_dump_json(indent=2))
    return 0 if isinstance(report, CodingBehaviorDryRunReport) or report.status == "passed" else 1


__all__ = [
    "BASELINE_SUITE_ID",
    "FIXED_SERVER_URL",
    "CodingBehaviorRealRunOptions",
    "CodingBehaviorRunnerConfigurationError",
    "IsolatedHeldOutValidationExecutor",
    "build_real_run_options",
    "load_baseline_suite",
    "main",
    "run_coding_behavior_eval",
    "write_result_artifact",
]


if __name__ == "__main__":
    raise SystemExit(main())
