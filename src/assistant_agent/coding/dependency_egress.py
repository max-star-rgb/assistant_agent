"""Fail-closed Docker lifecycle for approved public dependency downloads."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from assistant_agent.coding.config import CodingCredentialProfile, CodingDependencyProfile
from assistant_agent.coding.credentials import (
    CredentialBroker,
    credential_lease,
)
from assistant_agent.coding.dependencies import validate_wheelhouse
from assistant_agent.coding.models import (
    CodingCredentialRequest,
    CodingDependencyManifest,
    CodingDependencyPlan,
)


class DockerRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout: float): ...

    def run_input(
        self,
        argv: tuple[str, ...],
        *,
        input_bytes: bytearray,
        timeout: float,
    ): ...


class CodingDependencyFetcher(Protocol):
    def fetch(
        self,
        profile: CodingDependencyProfile,
        plan: CodingDependencyPlan,
        input_root: Path,
        output_root: Path,
        *,
        credential_profile: CodingCredentialProfile | None = None,
        credential_request: CodingCredentialRequest | None = None,
    ) -> CodingDependencyManifest: ...

    async def aclose(self) -> None: ...


class SubprocessDockerRunner:
    def run(self, argv: tuple[str, ...], *, timeout: float):
        return self._run(argv, timeout=timeout, input_bytes=None)

    def run_input(
        self,
        argv: tuple[str, ...],
        *,
        input_bytes: bytearray,
        timeout: float,
    ):
        if not input_bytes or len(input_bytes) > 16_384:
            raise subprocess.SubprocessError("Docker CLI input is invalid")
        return self._run(argv, timeout=timeout, input_bytes=input_bytes)

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        input_bytes: bytearray | None,
    ):
        process = subprocess.Popen(
            argv,
            stdin=(subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL),
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
            if input_bytes is not None:
                if process.stdin is None:
                    raise subprocess.SubprocessError("Docker CLI stdin unavailable")
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.flush()
                except BrokenPipeError as exc:
                    raise subprocess.SubprocessError("Docker CLI stdin failed") from exc
                finally:
                    process.stdin.close()
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
        credential_broker: CredentialBroker | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        self.docker = docker_binary
        self.runner = command_runner or SubprocessDockerRunner()
        self.owner = owner_id or uuid.uuid4().hex
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid
        self.names = name_factory or (lambda role: f"assistant-dependency-{role}-{uuid.uuid4().hex[:12]}")
        self.credential_broker = credential_broker

    def fetch(
        self,
        profile: CodingDependencyProfile,
        plan: CodingDependencyPlan,
        input_root: Path,
        output_root: Path,
        *,
        credential_profile: CodingCredentialProfile | None = None,
        credential_request: CodingCredentialRequest | None = None,
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
        private = credential_request is not None or credential_profile is not None
        if private and (
            credential_request is None
            or credential_profile is None
            or self.credential_broker is None
            or profile.credential_profile_id != credential_profile.credential_profile_id
        ):
            raise ValueError("credential_broker_unconfigured")
        if not private and profile.credential_profile_id is not None:
            raise ValueError("credential_approval_required")
        internal, external = self.names("internal"), self.names("external")
        proxy = self.names("gateway" if private else "proxy")
        downloader = self.names("downloader")
        resources: list[list[object]] = []
        cleanup_failed = False
        result: CodingDependencyManifest | None = None
        error = "dependency_fetch_failed"
        try:
            if private:
                self._image(
                    credential_profile.gateway_image,
                    "org.assistant-agent.coding-registry-gateway-protocol=1",
                )
            else:
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
            if private:
                self._ok(("create", "--name", proxy, "--hostname", "dependency-gateway", "--network", internal, *security, "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}", "--tmpfs", f"/run/assistant-agent-credentials:rw,noexec,nosuid,nodev,size=65536,mode=0700,uid={self.uid},gid={self.gid}", "--log-driver", "none", "--entrypoint", "/usr/local/bin/assistant-agent-registry-gateway", credential_profile.gateway_image, "--policy", "/policy.json", "--credential", "/run/assistant-agent-credentials/credential"), 20)
            else:
                self._ok(("create", "--name", proxy, "--hostname", "dependency-proxy", "--network", internal, *security, "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=16777216,uid={self.uid},gid={self.gid}", "--log-driver", "none", "--entrypoint", "/usr/local/bin/assistant-agent-dependency-proxy", profile.proxy_image, "--policy", "/policy.json"), 20)
            resources[-1][2] = True
            self._ok(("network", "connect", external, proxy), 20)
            resources.append(["container", downloader, False])
            route_env = (
                f"PIP_INDEX_URL=http://dependency-gateway:8080{credential_profile.registry_base_path}"
                if private
                else "HTTPS_PROXY=http://dependency-proxy:8080"
            )
            self._ok(("create", "--name", downloader, "--hostname", "dependency-fetch", "--network", internal, *security, "--tmpfs", f"/wheelhouse:rw,nosuid,nodev,size={plan.max_download_bytes},nr_inodes={plan.max_files + 32},uid={self.uid},gid={self.gid}", "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size=67108864,uid={self.uid},gid={self.gid}", "--env", route_env, "--log-driver", "none", "--entrypoint", "/usr/local/bin/assistant-agent-dependency-fetch", profile.downloader_image, "--lockfile", "/input/requirements.lock", "--output", "/wheelhouse"), 20)
            resources[-1][2] = True
            policy = {"hosts": list(plan.allowed_hosts), "ports": list(plan.allowed_ports), "max_bytes": plan.max_download_bytes}
            if private:
                policy.update(
                    registry_host=credential_profile.registry_host,
                    registry_base_path=credential_profile.registry_base_path,
                    auth_scheme=credential_profile.auth_scheme,
                )
            with tempfile.TemporaryDirectory() as temporary:
                policy_path = Path(temporary) / "policy.json"
                policy_path.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
                self._ok(("cp", "--archive", str(policy_path), f"{proxy}:/policy.json"), 20)
            self._ok(("cp", "--archive", str(input_root / plan.lockfile_path), f"{downloader}:/input/requirements.lock"), 20)
            self._ok(("start", proxy), 20)
            self._state(proxy, running=True, exit_code=0)
            with ExitStack() as stack:
                if private:
                    lease = stack.enter_context(
                        credential_lease(self.credential_broker, credential_request)
                    )
                    completed = self.runner.run_input(
                        (
                            self.docker,
                            "exec",
                            "-i",
                            proxy,
                            "/usr/local/bin/assistant-agent-credential-loader",
                        ),
                        input_bytes=lease.secret,
                        timeout=10,
                    )
                    if completed.returncode != 0:
                        raise ValueError("credential_gateway_unavailable")
                    ready = self.runner.run(
                        (
                            self.docker,
                            "exec",
                            proxy,
                            "/usr/local/bin/assistant-agent-registry-ready",
                        ),
                        timeout=10,
                    )
                    if ready.returncode != 0:
                        raise ValueError("credential_gateway_unavailable")
                self._ok(("start", downloader), 20)
                self._ok(("wait", downloader), profile.timeout_seconds)
                self._state(downloader, running=False, exit_code=0)
                output_root.mkdir(parents=True, exist_ok=True)
                self._ok(("cp", "--archive", f"{downloader}:/wheelhouse/.", str(output_root)), 30)
                result = validate_wheelhouse(plan, output_root)
                if private:
                    evidence = {
                        "credential_profile_id": credential_profile.credential_profile_id,
                        "credential_policy_digest": credential_request.credential_policy_digest,
                        "credential_request_digest": credential_request.request_digest,
                        "credential_lease_id_digest": hashlib.sha256(
                            lease.lease_id.encode("utf-8")
                        ).hexdigest(),
                        "credential_lease_issued_at": lease.issued_at,
                        "credential_lease_expires_at": lease.expires_at,
                        "credential_lease_status": "used",
                    }
                    candidate = result.model_copy(update=evidence)
                    manifest_values = candidate.model_dump(
                        mode="json", exclude={"manifest_digest"}
                    )
                    manifest_digest = hashlib.sha256(
                        json.dumps(
                            manifest_values,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    result = candidate.model_copy(
                        update={"manifest_digest": manifest_digest}
                    )
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
