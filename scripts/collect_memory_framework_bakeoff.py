"""Collect real Hindsight/Mem0 bake-off metrics with qwen-plus and text-embedding-v4.

Only anonymous IDs, stable error codes, latencies, and aggregate statistics are
written. Credentials and raw provider responses are never written to evidence.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.memory.framework.adapters import (
    HindsightMemoryEngineAdapter,
    Mem0MemoryEngineAdapter,
)
from assistant_agent.memory.framework.collector import (
    BakeoffCollectionAborted,
    BakeoffCollectionMeasurements,
    BakeoffFrameworkCollector,
    BakeoffLifecycleController,
    write_evidence,
)


CHAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CHAT_MODEL = "qwen-plus"
EMBEDDING_MODEL = "text-embedding-v4"
FIXED_VERSIONS = {"hindsight": "0.8.4", "mem0": "2.0.11"}
COMPOSE_FILE = ROOT / "docker/memory-frameworks/compose.yaml"


class BakeoffCliError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class DockerComposeLifecycle(BakeoffLifecycleController):
    """Own the dedicated bake-off Compose project and its disposable volumes."""

    def __init__(self, *, framework: str, api_key: str) -> None:
        self.framework = framework
        self.service = framework
        self.api_key = api_key
        self.base_url = "http://127.0.0.1:8889" if framework == "hindsight" else "http://127.0.0.1:8890"
        self._cold_start_seconds = 0.0
        self._peak_rss_mb = 0.0
        self._env = {
            **os.environ,
            "MEMORY_BAKEOFF_CHAT_BASE_URL": CHAT_BASE_URL,
            "MEMORY_BAKEOFF_CHAT_MODEL": CHAT_MODEL,
            "MEMORY_BAKEOFF_CHAT_API_KEY": api_key,
            "MEMORY_BAKEOFF_EMBEDDING_BASE_URL": CHAT_BASE_URL,
            "MEMORY_BAKEOFF_EMBEDDING_MODEL": EMBEDDING_MODEL,
            "MEMORY_BAKEOFF_EMBEDDING_API_KEY": api_key,
        }

    def preflight(self) -> None:
        self._run(["docker", "version", "--format", "{{.Server.Version}}"])
        self._run(["docker", "compose", "version"])
        self._compose("config", "--quiet")

    def reset_and_start(self) -> None:
        self._compose("down", "--volumes", "--remove-orphans", profile=self.framework)
        started = time.perf_counter()
        args = ["up", "-d", "--build"]
        args.append(self.service)
        self._compose(*args, profile=self.framework)
        self._wait_healthy()
        self._cold_start_seconds = time.perf_counter() - started

    def restart(self) -> None:
        self._compose("restart", self.service, profile=self.framework)
        self._wait_healthy()

    def stop(self) -> None:
        self._compose("stop", self.service, profile=self.framework)

    def start(self) -> None:
        self._compose("start", self.service, profile=self.framework)
        self._wait_healthy()

    def measurements(self) -> BakeoffCollectionMeasurements:
        self.sample_resources()
        container_ids = [self._container_id(self.service)]
        disk_paths = [(container_ids[0], "/home/hindsight/.pg0" if self.framework == "hindsight" else "/data/history")]
        if self.framework == "mem0":
            qdrant_id = self._container_id("qdrant")
            disk_paths.append((qdrant_id, "/qdrant/storage"))
        disk_mb = sum(self._disk_mb(container_id, path) for container_id, path in disk_paths)
        return BakeoffCollectionMeasurements(
            rss_mb=round(self._peak_rss_mb, 3),
            disk_mb=round(disk_mb, 3),
            cold_start_seconds=round(self._cold_start_seconds, 3),
            backup_portable=False,
            configuration_steps=7 if self.framework == "hindsight" else 9,
        )

    def sample_resources(self) -> None:
        try:
            container_ids = [self._container_id(self.service)]
            if self.framework == "mem0":
                container_ids.append(self._container_id("qdrant"))
            current = sum(self._rss_mb(container_id) for container_id in container_ids)
        except BakeoffCliError:
            return
        self._peak_rss_mb = max(self._peak_rss_mb, current)

    def _container_id(self, service: str) -> str:
        result = self._compose("ps", "--all", "-q", service, profile=self.framework)
        container_id = result.stdout.strip()
        if not container_id:
            raise BakeoffCliError("memory_bakeoff_container_missing")
        return container_id

    def _container_running(self, container_id: str) -> bool:
        result = self._run(["docker", "inspect", "--format", "{{.State.Running}}", container_id])
        return result.stdout.strip().lower() == "true"

    def _rss_mb(self, container_id: str) -> float:
        result = self._run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container_id]
        )
        used = result.stdout.partition("/")[0].strip()
        return _size_to_mb(used)

    def _disk_mb(self, container_id: str, path: str) -> float:
        result = self._run(["docker", "exec", container_id, "du", "-sm", path])
        token = result.stdout.strip().split(maxsplit=1)[0]
        try:
            return max(0.0, float(token))
        except ValueError as exc:
            raise BakeoffCliError("memory_bakeoff_disk_stat_invalid") from exc

    def _wait_healthy(self, *, timeout_seconds: float | None = None) -> None:
        timeout_seconds = timeout_seconds or _startup_timeout_seconds(self.framework)
        deadline = time.monotonic() + timeout_seconds
        health_url = f"{self.base_url}/health" if self.framework == "hindsight" else f"{self.base_url}/"
        container_id = self._container_id(self.service)
        while time.monotonic() < deadline:
            if not self._container_running(container_id):
                raise BakeoffCliError("memory_bakeoff_sidecar_exited")
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:
                    if 200 <= response.status < 300:
                        return
            except Exception:
                time.sleep(1)
        raise BakeoffCliError("memory_bakeoff_sidecar_start_timeout")

    def _compose(self, *args: str, profile: str | None = None) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "-f", str(COMPOSE_FILE)]
        if profile:
            command.extend(["--profile", profile])
        command.extend(args)
        return self._run(command)

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=self._env,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise BakeoffCliError("memory_bakeoff_docker_command_failed") from exc
        if completed.returncode != 0:
            raise BakeoffCliError("memory_bakeoff_docker_command_failed")
        return completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("smoke", "full"), required=True)
    parser.add_argument("--framework", choices=("hindsight", "mem0"), required=True)
    parser.add_argument("--evidence-dir", type=Path, default=Path(".local/memory-framework-bakeoff"))
    parser.add_argument("--version", help="Must match the fixed framework version.")
    args = parser.parse_args()

    expected_version = FIXED_VERSIONS[args.framework]
    version = args.version or expected_version
    try:
        _validate_runtime(version=version, expected_version=expected_version)
        api_key = os.environ["MEMORY_BAKEOFF_API_KEY"]
        lifecycle = DockerComposeLifecycle(framework=args.framework, api_key=api_key)
        lifecycle.preflight()
        lifecycle.reset_and_start()
        adapter = (
            HindsightMemoryEngineAdapter(base_url=lifecycle.base_url, timeout_seconds=30)
            if args.framework == "hindsight"
            else Mem0MemoryEngineAdapter(base_url=lifecycle.base_url, timeout_seconds=30)
        )
        with tempfile.TemporaryDirectory(prefix="assistant-agent-memory-bakeoff-") as runtime_dir:
            collector = BakeoffFrameworkCollector(
                framework=args.framework,
                phase=args.phase,
                adapter=adapter,
                ledger_path=Path(runtime_dir) / "governance.sqlite3",
                lifecycle=lifecycle,
            )
            result = collector.collect()
        evidence_path = args.evidence_dir / f"{args.framework}-{args.phase}-evidence.json"
        metrics_path = args.evidence_dir / f"{args.framework}-{args.phase}-metrics.json"
        write_evidence(evidence_path, result.model_dump(mode="json"))
        write_evidence(metrics_path, result.metrics.model_dump(mode="json"))
    except (BakeoffCliError, BakeoffCollectionAborted) as exc:
        print(json.dumps({"error_code": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except Exception:
        print(
            json.dumps({"error_code": "memory_bakeoff_collection_failed"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "framework": args.framework,
                "phase": args.phase,
                "evidence": str(evidence_path),
                "metrics": str(metrics_path),
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_runtime(*, version: str, expected_version: str) -> None:
    if version != expected_version:
        raise BakeoffCliError("memory_bakeoff_version_not_pinned")
    if os.environ.get("MULTIMODAL_AGENT_RUNTIME_PROFILE") not in {"pilot", "provider_smoke"}:
        raise BakeoffCliError("memory_bakeoff_profile_not_allowed")
    if not os.environ.get("MEMORY_BAKEOFF_API_KEY", "").strip():
        raise BakeoffCliError("memory_bakeoff_missing_api_key")


def _startup_timeout_seconds(framework: str) -> float:
    return 900.0 if framework == "hindsight" else 600.0


def _size_to_mb(value: str) -> float:
    normalized = value.strip().lower().replace("ib", "b")
    units = {"b": 1 / (1024 * 1024), "kb": 1 / 1024, "mb": 1.0, "gb": 1024.0}
    for unit in ("gb", "mb", "kb", "b"):
        if normalized.endswith(unit):
            try:
                return max(0.0, float(normalized[: -len(unit)].strip()) * units[unit])
            except ValueError as exc:
                raise BakeoffCliError("memory_bakeoff_rss_stat_invalid") from exc
    raise BakeoffCliError("memory_bakeoff_rss_stat_invalid")


if __name__ == "__main__":
    raise SystemExit(main())
