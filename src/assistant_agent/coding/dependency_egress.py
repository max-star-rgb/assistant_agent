"""Fail-closed Docker lifecycle for approved public dependency downloads."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from assistant_agent.coding.config import CodingDependencyProfile
from assistant_agent.coding.dependencies import validate_wheelhouse
from assistant_agent.coding.models import CodingDependencyManifest, CodingDependencyPlan


class DockerRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout: float): ...


class CodingDependencyFetcher(Protocol):
    def fetch(
        self,
        profile: CodingDependencyProfile,
        plan: CodingDependencyPlan,
        input_root: Path,
        output_root: Path,
    ) -> CodingDependencyManifest: ...

    async def aclose(self) -> None: ...


class SubprocessDockerRunner:
    def run(self, argv: tuple[str, ...], *, timeout: float):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


class DockerDependencyFetcher:
    def __init__(
        self,
        docker_binary: str = "docker",
        *,
        command_runner: DockerRunner | None = None,
        owner_id: str | None = None,
        name_factory: Callable[[str], str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        self.docker = docker_binary
        self.runner = command_runner or SubprocessDockerRunner()
        self.owner = owner_id or uuid.uuid4().hex
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid
        self.names = name_factory or (lambda role: f"assistant-dependency-{role}-{uuid.uuid4().hex[:12]}")

    def fetch(
        self,
        profile: CodingDependencyProfile,
        plan: CodingDependencyPlan,
        input_root: Path,
        output_root: Path,
    ) -> CodingDependencyManifest:
        if profile.profile_id != plan.profile_id:
            raise ValueError("dependency_approval_mismatch")
        if output_root.exists() and (
            output_root.is_symlink()
            or not output_root.is_dir()
            or any(output_root.iterdir())
        ):
            raise ValueError("dependency_artifact_invalid")
        internal, external = self.names("internal"), self.names("external")
        proxy, downloader = self.names("proxy"), self.names("downloader")
        resources: list[tuple[str, str]] = []
        cleanup_failed = False
        result: CodingDependencyManifest | None = None
        error = "dependency_fetch_failed"
        try:
            self._image(profile.proxy_image, "org.assistant-agent.coding-egress-proxy-protocol=1")
            self._image(profile.downloader_image, "org.assistant-agent.coding-dependency-fetch-protocol=1")
            self._ok(("network", "create", "--internal", "--label", f"assistant_agent.coding.owner={self.owner}", internal), 20)
            resources.append(("network", internal))
            self._ok(("network", "create", "--label", f"assistant_agent.coding.owner={self.owner}", external), 20)
            resources.append(("network", external))
            security = (
                "--read-only", "--user", f"{self.uid}:{self.gid}",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--memory", "268435456", "--memory-swap", "268435456",
                "--cpus", "1.0", "--pids-limit", "64",
            )
            self._ok(("create", "--name", proxy, "--hostname", "dependency-proxy", "--network", internal, *security, "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}", profile.proxy_image, "/usr/local/bin/assistant-agent-dependency-proxy", "--policy", "/policy.json"), 20)
            resources.append(("container", proxy))
            self._ok(("network", "connect", external, proxy), 20)
            self._ok(("create", "--name", downloader, "--hostname", "dependency-fetch", "--network", internal, *security, "--tmpfs", f"/wheelhouse:rw,nosuid,nodev,size={plan.max_download_bytes},nr_inodes={plan.max_files + 32},uid={self.uid},gid={self.gid}", "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=67108864,uid={self.uid},gid={self.gid}", "--env", "HTTPS_PROXY=http://dependency-proxy:8080", profile.downloader_image, "/usr/local/bin/assistant-agent-dependency-fetch", "--lockfile", "/input/requirements.lock", "--output", "/wheelhouse"), 20)
            resources.append(("container", downloader))
            policy = {"hosts": list(plan.allowed_hosts), "ports": list(plan.allowed_ports), "max_bytes": plan.max_download_bytes}
            with tempfile.TemporaryDirectory() as temporary:
                policy_path = Path(temporary) / "policy.json"
                policy_path.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
                self._ok(("cp", "--archive", str(policy_path), f"{proxy}:/policy.json"), 20)
            self._ok(("cp", "--archive", str(input_root / plan.lockfile_path), f"{downloader}:/input/requirements.lock"), 20)
            self._ok(("start", proxy), 20)
            self._state(proxy, running=True, exit_code=0)
            self._ok(("start", "--attach", downloader), profile.timeout_seconds)
            self._state(downloader, running=False, exit_code=0)
            output_root.mkdir(parents=True, exist_ok=True)
            self._ok(("cp", "--archive", f"{downloader}:/wheelhouse/.", str(output_root)), 30)
            result = validate_wheelhouse(plan, output_root)
        except ValueError as exc:
            error = str(exc)
        except (OSError, subprocess.SubprocessError):
            error = "dependency_fetch_failed"
        finally:
            for kind, name in reversed(resources):
                argv = ("rm", "--force", name) if kind == "container" else ("network", "rm", name)
                try:
                    completed = self.runner.run((self.docker, *argv), timeout=20)
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
                else:
                    cleanup_failed |= completed.returncode != 0
        if cleanup_failed:
            raise ValueError("dependency_cleanup_failed")
        if result is None:
            raise ValueError(error)
        return result

    def _image(self, image: str, label: str) -> None:
        completed = self.runner.run((self.docker, "image", "inspect", image), timeout=15)
        if completed.returncode != 0 or image not in completed.stdout or label not in completed.stdout:
            raise ValueError("dependency_egress_unconfigured")

    def _ok(self, argv: tuple[str, ...], timeout: float) -> None:
        completed = self.runner.run((self.docker, *argv), timeout=timeout)
        if completed.returncode != 0:
            raise ValueError("dependency_fetch_failed")

    def _state(self, name: str, *, running: bool, exit_code: int) -> None:
        completed = self.runner.run(
            (self.docker, "inspect", "--format", "{{json .State}}", name),
            timeout=10,
        )
        try:
            state = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("dependency_fetch_failed") from exc
        if (
            completed.returncode != 0
            or state.get("Running") is not running
            or state.get("ExitCode") != exit_code
            or state.get("OOMKilled") is not False
        ):
            raise ValueError("dependency_fetch_failed")

    async def aclose(self) -> None:
        for kind, list_argv, remove_prefix in (
            (
                "container",
                ("ps", "-aq", "--filter", f"label=assistant_agent.coding.owner={self.owner}"),
                ("rm", "--force"),
            ),
            (
                "network",
                ("network", "ls", "-q", "--filter", f"label=assistant_agent.coding.owner={self.owner}"),
                ("network", "rm"),
            ),
        ):
            try:
                listed = self.runner.run((self.docker, *list_argv), timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            if listed.returncode != 0:
                continue
            for name in listed.stdout.splitlines():
                if name.strip():
                    try:
                        self.runner.run(
                            (self.docker, *remove_prefix, name.strip()),
                            timeout=20,
                        )
                    except (OSError, subprocess.SubprocessError):
                        continue


__all__ = ["CodingDependencyFetcher", "DockerDependencyFetcher"]
