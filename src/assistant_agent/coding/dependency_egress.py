"""Fail-closed Docker lifecycle for approved public dependency downloads."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
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
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        buffers = [bytearray(), bytearray()]
        exceeded = threading.Event()

        def collect(stream, target: bytearray) -> None:
            while chunk := stream.read(65_536):
                if len(target) + len(chunk) > 1_048_576:
                    target.extend(chunk[: max(0, 1_048_576 - len(target))])
                    exceeded.set()
                    process.kill()
                    return
                target.extend(chunk)

        threads = [
            threading.Thread(target=collect, args=(process.stdout, buffers[0]), daemon=True),
            threading.Thread(target=collect, args=(process.stderr, buffers[1]), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        for thread in threads:
            thread.join(timeout=5)
        if exceeded.is_set() or any(thread.is_alive() for thread in threads):
            raise subprocess.SubprocessError("Docker CLI output exceeded limit")
        return subprocess.CompletedProcess(
            argv,
            return_code,
            buffers[0].decode(errors="replace"),
            buffers[1].decode(errors="replace"),
        )


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
        if self.uid <= 0 or self.gid < 0:
            raise ValueError("dependency_user_invalid")
        if output_root.exists() and (
            output_root.is_symlink()
            or not output_root.is_dir()
            or any(output_root.iterdir())
        ):
            raise ValueError("dependency_artifact_invalid")
        internal, external = self.names("internal"), self.names("external")
        proxy, downloader = self.names("proxy"), self.names("downloader")
        resources: list[list[object]] = []
        cleanup_failed = False
        result: CodingDependencyManifest | None = None
        error = "dependency_fetch_failed"
        try:
            self._image(profile.proxy_image, "org.assistant-agent.coding-egress-proxy-protocol=1")
            self._image(profile.downloader_image, "org.assistant-agent.coding-dependency-fetch-protocol=1")
            resources.append(["network", internal, False])
            self._ok(("network", "create", "--internal", "--label", f"assistant_agent.coding.owner={self.owner}", internal), 20)
            resources[-1][2] = True
            resources.append(["network", external, False])
            self._ok(("network", "create", "--label", f"assistant_agent.coding.owner={self.owner}", external), 20)
            resources[-1][2] = True
            security = (
                "--read-only", "--user", f"{self.uid}:{self.gid}",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--memory", "268435456", "--memory-swap", "268435456",
                "--cpus", "1.0", "--pids-limit", "64",
            )
            resources.append(["container", proxy, False])
            self._ok(("create", "--name", proxy, "--hostname", "dependency-proxy", "--network", internal, *security, "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}", "--log-driver", "none", "--entrypoint", "/usr/local/bin/assistant-agent-dependency-proxy", profile.proxy_image, "--policy", "/policy.json"), 20)
            resources[-1][2] = True
            self._ok(("network", "connect", external, proxy), 20)
            resources.append(["container", downloader, False])
            self._ok(("create", "--name", downloader, "--hostname", "dependency-fetch", "--network", internal, *security, "--tmpfs", f"/wheelhouse:rw,nosuid,nodev,size={plan.max_download_bytes},nr_inodes={plan.max_files + 32},uid={self.uid},gid={self.gid}", "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=67108864,uid={self.uid},gid={self.gid}", "--env", "HTTPS_PROXY=http://dependency-proxy:8080", "--log-driver", "none", "--entrypoint", "/usr/local/bin/assistant-agent-dependency-fetch", profile.downloader_image, "--lockfile", "/input/requirements.lock", "--output", "/wheelhouse"), 20)
            resources[-1][2] = True
            policy = {"hosts": list(plan.allowed_hosts), "ports": list(plan.allowed_ports), "max_bytes": plan.max_download_bytes}
            with tempfile.TemporaryDirectory() as temporary:
                policy_path = Path(temporary) / "policy.json"
                policy_path.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
                self._ok(("cp", "--archive", str(policy_path), f"{proxy}:/policy.json"), 20)
            self._ok(("cp", "--archive", str(input_root / plan.lockfile_path), f"{downloader}:/input/requirements.lock"), 20)
            self._ok(("start", proxy), 20)
            self._state(proxy, running=True, exit_code=0)
            self._ok(("start", downloader), 20)
            self._ok(("wait", downloader), profile.timeout_seconds)
            self._state(downloader, running=False, exit_code=0)
            output_root.mkdir(parents=True, exist_ok=True)
            self._ok(("cp", "--archive", f"{downloader}:/wheelhouse/.", str(output_root)), 30)
            result = validate_wheelhouse(plan, output_root)
        except ValueError as exc:
            error = str(exc)
        except (OSError, subprocess.SubprocessError):
            error = "dependency_fetch_failed"
        finally:
            for kind, name, confirmed in reversed(resources):
                argv = ("rm", "--force", name) if kind == "container" else ("network", "rm", name)
                try:
                    completed = self.runner.run((self.docker, *argv), timeout=20)
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
                else:
                    if completed.returncode != 0:
                        absent = "no such" in completed.stderr.lower()
                        cleanup_failed |= bool(confirmed) or not absent
        if cleanup_failed:
            raise ValueError("dependency_cleanup_failed")
        if result is None:
            raise ValueError(error)
        return result

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
        lines = completed.stdout.splitlines()
        try:
            digests = json.loads(lines[0])
            labels = json.loads(lines[1])
            key, expected = label.split("=", 1)
        except (IndexError, json.JSONDecodeError, ValueError):
            raise ValueError("dependency_egress_unconfigured")
        if (
            completed.returncode != 0
            or not isinstance(digests, list)
            or image not in digests
            or not isinstance(labels, dict)
            or labels.get(key) != expected
        ):
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
