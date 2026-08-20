"""Docker-isolated fetch and scan lifecycle for governed coding artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from assistant_agent.coding.artifacts import validate_artifact_bundle
from assistant_agent.coding.config import CodingArtifactProfile
from assistant_agent.coding.dependency_egress import DockerRunner, SubprocessDockerRunner
from assistant_agent.coding.models import (
    CodingArtifactIngressManifest,
    CodingArtifactIngressPlan,
    CodingArtifactExportManifest,
    CodingArtifactExportRecord,
    CodingArtifactExportRequest,
)


class ArtifactIngressBackend(Protocol):
    def fetch_scan(
        self,
        profile: CodingArtifactProfile,
        plan: CodingArtifactIngressPlan,
        input_root: Path,
        output_root: Path,
    ) -> CodingArtifactIngressManifest: ...

    def scan_exports(
        self,
        profile: CodingArtifactProfile,
        command_id: str,
        exports: tuple[CodingArtifactExportRequest, ...],
        input_root: Path,
        bundle_root: Path,
    ) -> CodingArtifactExportManifest: ...

    async def aclose(self) -> None: ...


class _ScanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["clean", "rejected"]


class _ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    protocol_version: Literal[1]
    scanner_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[_ScanItem, ...]

    @field_validator("artifacts", mode="before")
    @classmethod
    def _tuple_artifacts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class DockerArtifactIngressBackend:
    def __init__(
        self,
        docker_binary: str = "docker",
        *,
        command_runner: DockerRunner | None = None,
        owner_id: str | None = None,
        name_factory: Callable[[str], str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
        managed_bundle_root: Path | None = None,
        janitor_max_interval_seconds: float = 60.0,
    ) -> None:
        self.docker = docker_binary
        self.runner = command_runner or SubprocessDockerRunner()
        self.owner = owner_id or uuid.uuid4().hex
        self.names = name_factory or (
            lambda role: f"assistant-artifact-{role}-{uuid.uuid4().hex[:12]}"
        )
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid
        self._bundle_roots: set[Path] = (
            {managed_bundle_root} if managed_bundle_root is not None else set()
        )
        self._janitor_max_interval = max(1.0, janitor_max_interval_seconds)
        self._janitor_stop = threading.Event()
        self._janitor_wake = threading.Event()
        self._janitor_lock = threading.Lock()
        self._bundle_io_lock = threading.Lock()
        self._janitor: threading.Thread | None = None
        if managed_bundle_root is not None:
            self._start_janitor()

    def fetch_scan(
        self,
        profile: CodingArtifactProfile,
        plan: CodingArtifactIngressPlan,
        input_root: Path,
        output_root: Path,
    ) -> CodingArtifactIngressManifest:
        if profile.profile_id != plan.profile_id:
            raise ValueError("artifact_approval_mismatch")
        if self.uid <= 0 or self.gid < 0:
            raise ValueError("artifact_unconfigured")
        if output_root.exists() and (
            output_root.is_symlink()
            or not output_root.is_dir()
            or any(output_root.iterdir())
        ):
            raise ValueError("artifact_fetch_failed")
        internal, external = self.names("internal"), self.names("external")
        proxy, fetcher, scanner = (
            self.names("proxy"),
            self.names("fetcher"),
            self.names("scanner"),
        )
        resources: list[tuple[str, str]] = []
        result: CodingArtifactIngressManifest | None = None
        error = "artifact_fetch_failed"
        cleanup_failed = False
        scanner_policy_digest = hashlib.sha256(
            json.dumps(
                {
                    "scanner_image": profile.scanner_image,
                    "allowed_media_types": profile.allowed_media_types,
                    "max_file_bytes": profile.max_file_bytes,
                    "max_total_bytes": profile.max_total_bytes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        security = (
            "--read-only",
            "--user",
            f"{self.uid}:{self.gid}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--memory",
            "268435456",
            "--memory-swap",
            "268435456",
            "--cpus",
            "1.0",
            "--pids-limit",
            "64",
            "--label",
            f"assistant_agent.coding.owner={self.owner}",
            "--log-driver",
            "none",
        )
        try:
            self._image(
                profile.proxy_image,
                "org.assistant-agent.coding-egress-proxy-protocol",
            )
            self._image(
                profile.fetcher_image,
                "org.assistant-agent.coding-artifact-fetch-protocol",
            )
            self._image(
                profile.scanner_image,
                "org.assistant-agent.coding-artifact-scan-protocol",
            )
            resources.append(("network", internal))
            self._ok(
                (
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    f"assistant_agent.coding.owner={self.owner}",
                    internal,
                ),
                20,
            )
            resources.append(("network", external))
            self._ok(
                (
                    "network",
                    "create",
                    "--label",
                    f"assistant_agent.coding.owner={self.owner}",
                    external,
                ),
                20,
            )
            resources.append(("container", proxy))
            self._ok(
                (
                    "create",
                    "--name",
                    proxy,
                    "--hostname",
                    "artifact-proxy",
                    "--network",
                    internal,
                    *security,
                    "--tmpfs",
                    f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}",
                    "--entrypoint",
                    "/usr/local/bin/assistant-agent-dependency-proxy",
                    profile.proxy_image,
                    "--policy",
                    "/policy.json",
                ),
                20,
            )
            self._ok(("network", "connect", external, proxy), 20)
            resources.append(("container", fetcher))
            self._ok(
                (
                    "create",
                    "--name",
                    fetcher,
                    "--hostname",
                    "artifact-fetch",
                    "--network",
                    internal,
                    *security,
                    "--tmpfs",
                    f"/artifacts:rw,nosuid,nodev,size={plan.max_total_bytes},nr_inodes={plan.artifact_count + 16},uid={self.uid},gid={self.gid}",
                    "--env",
                    "HTTPS_PROXY=http://artifact-proxy:8080",
                    "--entrypoint",
                    "/usr/local/bin/assistant-agent-artifact-fetch",
                    profile.fetcher_image,
                    "--manifest",
                    "/input/artifacts.lock.json",
                    "--output",
                    "/artifacts",
                ),
                20,
            )
            policy = {
                "hosts": list(plan.allowed_hosts),
                "ports": list(plan.allowed_ports),
                "max_bytes": plan.max_total_bytes,
            }
            with tempfile.TemporaryDirectory() as temporary:
                policy_path = Path(temporary) / "policy.json"
                policy_path.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
                self._ok(("cp", "--archive", str(policy_path), f"{proxy}:/policy.json"), 20)
            resources.append(("container", scanner))
            self._ok(
                (
                    "cp",
                    "--archive",
                    str(input_root / profile.manifest_path),
                    f"{fetcher}:/input/artifacts.lock.json",
                ),
                20,
            )
            self._ok(("start", proxy), 20)
            self._state(proxy, running=True)
            self._ok(("start", fetcher), 20)
            self._ok(("wait", fetcher), plan.timeout_seconds)
            self._state(fetcher, running=False)
            output_root.mkdir(parents=True, exist_ok=True)
            self._ok(("cp", "--archive", f"{fetcher}:/artifacts/.", str(output_root)), 30)
            candidate = validate_artifact_bundle(
                plan,
                output_root,
                scanner_policy_digest=scanner_policy_digest,
            )
            self._ok(
                (
                    "create",
                    "--name",
                    scanner,
                    "--hostname",
                    "artifact-scan",
                    "--network",
                    "none",
                    *security,
                    "--tmpfs",
                    f"/scan:rw,nosuid,nodev,size={plan.max_total_bytes},nr_inodes={plan.artifact_count + 16},uid={self.uid},gid={self.gid}",
                    "--tmpfs",
                    f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}",
                    "--entrypoint",
                    "/usr/local/bin/assistant-agent-artifact-scan",
                    profile.scanner_image,
                    "--input",
                    "/scan",
                    "--result",
                    "/result.json",
                    "--policy-digest",
                    scanner_policy_digest,
                ),
                20,
            )
            self._ok(("cp", "--archive", f"{output_root}/.", f"{scanner}:/scan"), 30)
            self._ok(("start", scanner), 20)
            self._ok(("wait", scanner), plan.timeout_seconds)
            self._state(scanner, running=False)
            with tempfile.TemporaryDirectory() as temporary:
                result_path = Path(temporary) / "result.json"
                self._ok(("cp", "--archive", f"{scanner}:/result.json", str(result_path)), 20)
                scan = _read_scan_result(result_path, scanner_policy_digest)
            expected = {item.artifact_id: item.sha256 for item in plan.artifacts}
            observed = {item.artifact_id: item.sha256 for item in scan.artifacts}
            if (
                observed != expected
                or any(item.status != "clean" for item in scan.artifacts)
            ):
                raise ValueError("artifact_scan_failed")
            result = candidate
        except ValueError as exc:
            error = str(exc)
        except (OSError, subprocess.SubprocessError):
            error = "artifact_fetch_failed"
        finally:
            for kind, name in reversed(resources):
                argv = (
                    ("rm", "--force", name)
                    if kind == "container"
                    else ("network", "rm", name)
                )
                try:
                    removed = self.runner.run((self.docker, *argv), timeout=20)
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
                else:
                    cleanup_failed |= removed.returncode != 0
        if cleanup_failed:
            raise ValueError("artifact_cleanup_failed")
        if result is None:
            raise ValueError(error)
        return result

    def scan_exports(
        self,
        profile: CodingArtifactProfile,
        command_id: str,
        exports: tuple[CodingArtifactExportRequest, ...],
        input_root: Path,
        bundle_root: Path,
    ) -> CodingArtifactExportManifest:
        if self.uid <= 0 or self.gid < 0:
            raise ValueError("artifact_unconfigured")
        configured = {
            item.export_id: item
            for item in profile.exports.values()
            if item.command_id == command_id
        }
        if not exports or set(configured) != {item.export_id for item in exports}:
            raise ValueError("artifact_export_invalid")
        records: list[CodingArtifactExportRecord] = []
        total_bytes = 0
        for export in exports:
            policy = configured.get(export.export_id)
            if (
                policy is None
                or policy.path != export.path
                or policy.media_type != export.media_type
                or policy.max_bytes != export.max_bytes
            ):
                raise ValueError("artifact_export_invalid")
            path = input_root.joinpath(*export.path.split("/"))
            current = input_root
            for part in export.path.split("/")[:-1]:
                current /= part
                if current.is_symlink() or not current.is_dir():
                    raise ValueError("artifact_export_invalid")
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ValueError("artifact_export_invalid") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size <= 0
                or metadata.st_size > export.max_bytes
            ):
                raise ValueError("artifact_export_invalid")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            total_bytes += metadata.st_size
            records.append(
                CodingArtifactExportRecord(
                    export_id=export.export_id,
                    path=export.path,
                    media_type=export.media_type,
                    size_bytes=metadata.st_size,
                    sha256=digest,
                )
            )
        if total_bytes > profile.max_total_bytes:
            raise ValueError("artifact_export_invalid")
        scanner_policy_digest = hashlib.sha256(
            json.dumps(
                {
                    "scanner_image": profile.scanner_image,
                    "exports": [item.model_dump(mode="json") for item in exports],
                    "max_total_bytes": profile.max_total_bytes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        scanner = self.names("export-scanner")
        created = False
        cleanup_failed = False
        try:
            self._image(
                profile.scanner_image,
                "org.assistant-agent.coding-artifact-scan-protocol",
            )
            created = True
            self._ok(
                (
                    "create",
                    "--name",
                    scanner,
                    "--hostname",
                    "artifact-export-scan",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    f"{self.uid}:{self.gid}",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--memory",
                    "268435456",
                    "--memory-swap",
                    "268435456",
                    "--cpus",
                    "1.0",
                    "--pids-limit",
                    "64",
                    "--label",
                    f"assistant_agent.coding.owner={self.owner}",
                    "--log-driver",
                    "none",
                    "--tmpfs",
                    f"/scan:rw,nosuid,nodev,size={max(total_bytes, 1048576)},nr_inodes={len(records) + 16},uid={self.uid},gid={self.gid}",
                    "--tmpfs",
                    f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}",
                    "--entrypoint",
                    "/usr/local/bin/assistant-agent-artifact-scan",
                    profile.scanner_image,
                    "--input",
                    "/scan",
                    "--result",
                    "/result.json",
                    "--policy-digest",
                    scanner_policy_digest,
                ),
                20,
            )
            self._ok(("cp", "--archive", f"{input_root}/.", f"{scanner}:/scan"), 30)
            self._ok(("start", scanner), 20)
            self._ok(("wait", scanner), profile.timeout_seconds)
            self._state(scanner, running=False)
            with tempfile.TemporaryDirectory() as temporary:
                result_path = Path(temporary) / "result.json"
                self._ok(("cp", "--archive", f"{scanner}:/result.json", str(result_path)), 20)
                scan = _read_scan_result(result_path, scanner_policy_digest)
            expected = {item.export_id: item.sha256 for item in records}
            observed = {item.artifact_id: item.sha256 for item in scan.artifacts}
            if observed != expected or any(
                item.status != "clean" for item in scan.artifacts
            ):
                raise ValueError("artifact_scan_failed")
        except ValueError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("artifact_scan_failed") from exc
        finally:
            if created:
                try:
                    removed = self.runner.run(
                        (self.docker, "rm", "--force", scanner), timeout=20
                    )
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
                else:
                    cleanup_failed = removed.returncode != 0
            if cleanup_failed:
                raise ValueError("artifact_cleanup_failed")
        return self._persist_export_bundle(
            profile,
            command_id,
            tuple(records),
            input_root,
            bundle_root,
            scanner_policy_digest,
        )

    def _persist_export_bundle(
        self,
        profile: CodingArtifactProfile,
        command_id: str,
        records: tuple[CodingArtifactExportRecord, ...],
        input_root: Path,
        bundle_root: Path,
        scanner_policy_digest: str,
    ) -> CodingArtifactExportManifest:
        bundle_root.mkdir(parents=True, exist_ok=True)
        if bundle_root.is_symlink() or not bundle_root.is_dir():
            raise ValueError("artifact_export_failed")
        now = datetime.now(UTC)
        with self._bundle_io_lock:
            self._prune_expired_bundles(bundle_root, now)
        with self._janitor_lock:
            self._bundle_roots.add(bundle_root)
        self._start_janitor()
        bundle_ref = f"artifact_bundle_{uuid.uuid4().hex}"
        destination = bundle_root / bundle_ref
        staging = bundle_root / f".{bundle_ref}.tmp"
        expires_at = now + timedelta(seconds=profile.bundle_ttl_seconds)
        manifest_values = {
            "profile_id": profile.profile_id,
            "command_id": command_id,
            "artifacts": [item.model_dump(mode="json") for item in records],
            "artifact_count": len(records),
            "total_bytes": sum(item.size_bytes for item in records),
            "scanner_policy_digest": scanner_policy_digest,
            "bundle_ref": bundle_ref,
            "created_at": now,
            "expires_at": expires_at,
        }
        disk_payload = {
            **manifest_values,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        manifest_digest = hashlib.sha256(
            json.dumps(disk_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        disk_payload["manifest_digest"] = manifest_digest
        manifest_values["manifest_digest"] = manifest_digest
        try:
            staging.mkdir(mode=0o700)
            for record in records:
                source = input_root.joinpath(*record.path.split("/"))
                target = staging.joinpath(*record.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target, follow_symlinks=False)
                target.chmod(0o400)
            metadata = staging / "manifest.json"
            metadata.write_text(
                json.dumps(disk_payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            metadata.chmod(0o400)
            staging.rename(destination)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError("artifact_export_failed") from exc
        self._janitor_wake.set()
        return CodingArtifactExportManifest.model_validate(manifest_values)

    def _prune_expired_bundles(
        self, bundle_root: Path, now: datetime
    ) -> datetime | None:
        if not bundle_root.exists():
            return None
        next_due: datetime | None = None
        try:
            entries = tuple(bundle_root.iterdir())
            for entry in entries:
                if entry.name.startswith("."):
                    due = datetime.fromtimestamp(
                        entry.stat().st_mtime + 300, tz=UTC
                    )
                    if due <= now:
                        if entry.is_symlink() or not entry.is_dir():
                            raise ValueError("artifact_cleanup_failed")
                        shutil.rmtree(entry)
                    elif next_due is None or due < next_due:
                        next_due = due
                    continue
                if (
                    entry.is_symlink()
                    or not entry.is_dir()
                    or not entry.name.startswith("artifact_bundle_")
                    or len(entry.name) != len("artifact_bundle_") + 32
                ):
                    raise ValueError("artifact_cleanup_failed")
                metadata = entry / "manifest.json"
                if metadata.is_symlink() or metadata.stat().st_size > 262_144:
                    raise ValueError("artifact_cleanup_failed")
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                if payload.get("bundle_ref") != entry.name:
                    raise ValueError("artifact_cleanup_failed")
                expires_at = datetime.fromisoformat(payload["expires_at"])
                if expires_at.tzinfo is None:
                    raise ValueError("artifact_cleanup_failed")
                if expires_at <= now:
                    shutil.rmtree(entry)
                elif next_due is None or expires_at < next_due:
                    next_due = expires_at
        except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
            raise ValueError("artifact_cleanup_failed") from exc
        return next_due

    def _start_janitor(self) -> None:
        with self._janitor_lock:
            if self._janitor is not None:
                return
            self._janitor = threading.Thread(
                target=self._janitor_loop,
                name=f"artifact-janitor-{self.owner[:16]}",
                daemon=True,
            )
            self._janitor.start()

    def _janitor_loop(self) -> None:
        while not self._janitor_stop.is_set():
            now = datetime.now(UTC)
            next_due: datetime | None = None
            with self._janitor_lock:
                roots = tuple(self._bundle_roots)
            for bundle_root in roots:
                try:
                    with self._bundle_io_lock:
                        candidate = self._prune_expired_bundles(bundle_root, now)
                except ValueError:
                    continue
                if candidate is not None and (
                    next_due is None or candidate < next_due
                ):
                    next_due = candidate
            wait_seconds = self._janitor_max_interval
            if next_due is not None:
                wait_seconds = min(
                    wait_seconds,
                    max(0.1, (next_due - datetime.now(UTC)).total_seconds()),
                )
            self._janitor_wake.wait(wait_seconds)
            self._janitor_wake.clear()

    def _image(self, image: str, label: str) -> None:
        completed = self.runner.run(
            (
                self.docker,
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}\n{{json .Config.Labels}}",
                image,
            ),
            timeout=15,
        )
        try:
            digests, labels = (
                json.loads(completed.stdout.splitlines()[0]),
                json.loads(completed.stdout.splitlines()[1]),
            )
        except (IndexError, json.JSONDecodeError):
            raise ValueError("artifact_unconfigured")
        if (
            completed.returncode != 0
            or image not in digests
            or not isinstance(labels, dict)
            or labels.get(label) != "1"
        ):
            raise ValueError("artifact_unconfigured")

    def _ok(self, argv: tuple[str, ...], timeout: float) -> None:
        completed = self.runner.run((self.docker, *argv), timeout=timeout)
        if completed.returncode != 0:
            raise ValueError("artifact_fetch_failed")

    def _state(self, name: str, *, running: bool) -> None:
        completed = self.runner.run(
            (self.docker, "inspect", "--format", "{{json .State}}", name),
            timeout=10,
        )
        try:
            state = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("artifact_fetch_failed") from exc
        if (
            completed.returncode != 0
            or state.get("Running") is not running
            or state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
        ):
            raise ValueError("artifact_fetch_failed")

    async def aclose(self) -> None:
        self._janitor_stop.set()
        self._janitor_wake.set()
        janitor = self._janitor
        if janitor is not None:
            janitor.join(timeout=5)
        now = datetime.now(UTC)
        with self._janitor_lock:
            bundle_roots = tuple(self._bundle_roots)
        for bundle_root in bundle_roots:
            try:
                with self._bundle_io_lock:
                    self._prune_expired_bundles(bundle_root, now)
            except ValueError:
                pass
        for list_argv, remove_prefix in (
            (
                (
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=assistant_agent.coding.owner={self.owner}",
                ),
                ("rm", "--force"),
            ),
            (
                (
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=assistant_agent.coding.owner={self.owner}",
                ),
                ("network", "rm"),
            ),
        ):
            try:
                listed = self.runner.run((self.docker, *list_argv), timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            for name in listed.stdout.splitlines() if listed.returncode == 0 else ():
                if name.strip():
                    try:
                        self.runner.run(
                            (self.docker, *remove_prefix, name.strip()), timeout=20
                        )
                    except (OSError, subprocess.SubprocessError):
                        continue


def _read_scan_result(path: Path, expected_policy: str) -> _ScanResult:
    try:
        if path.stat().st_size > 262_144:
            raise ValueError("artifact_scan_failed")
        result = _ScanResult.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ValueError("artifact_scan_failed") from exc
    if result.scanner_policy_digest != expected_policy:
        raise ValueError("artifact_scan_failed")
    ids = [item.artifact_id for item in result.artifacts]
    if len(ids) != len(set(ids)):
        raise ValueError("artifact_scan_failed")
    return result


__all__ = ["ArtifactIngressBackend", "DockerArtifactIngressBackend"]
