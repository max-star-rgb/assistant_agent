#!/usr/bin/env python3
"""Start the repository-local Mem0 + Qdrant stack."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
COMPOSE_FILE = REPO_ROOT / "docker" / "mem0" / "compose.yaml"
HEALTH_URL = "http://127.0.0.1:8890/ready"
STARTUP_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 1.0


def main() -> int:
    missing = [path for path in (ENV_FILE, COMPOSE_FILE) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"Required file not found: {path}", file=sys.stderr)
        return 2

    try:
        _compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "mem0",
            "qdrant",
            timeout=STARTUP_TIMEOUT_SECONDS,
        )
        container_id = _compose_output("ps", "-q", "mem0")
        if not container_id:
            print("Mem0 container was not created.", file=sys.stderr)
            return 1

        status = _wait_until_healthy(container_id)
        if status != "healthy":
            print(
                f"Mem0 did not become healthy (container status: {status}).",
                file=sys.stderr,
            )
            _print_diagnostics_hint()
            return 1

        payload = _read_health()
        version = str(payload.get("version") or "unknown")
        framework = str(payload.get("framework") or "mem0")
        print(
            f"Mem0 ready: {HEALTH_URL} (framework {framework}, version {version})",
            flush=True,
        )
        print("Mem0 and Qdrant will continue running after this script exits.", flush=True)
        print(f"Stop them with: {_stop_command()}", flush=True)
        return 0
    except KeyboardInterrupt:
        print(
            "\nStartup wait interrupted; any containers already started remain running.",
            file=sys.stderr,
        )
        return 130
    except subprocess.TimeoutExpired:
        print("Timed out while starting Mem0.", file=sys.stderr)
        _print_diagnostics_hint()
        return 1
    except (OSError, subprocess.CalledProcessError, URLError, ValueError) as exc:
        print(f"Failed to start Mem0: {exc}", file=sys.stderr)
        _print_diagnostics_hint()
        return 1


def _compose(*args: str, timeout: float | None = None) -> None:
    subprocess.run(
        _compose_command(*args),
        cwd=REPO_ROOT,
        check=True,
        timeout=timeout,
    )


def _compose_output(*args: str) -> str:
    completed = subprocess.run(
        _compose_command(*args),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "mem0",
        *args,
    ]


def _wait_until_healthy(container_id: str) -> str:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    status = "unknown"
    while time.monotonic() < deadline:
        status = _container_status(container_id)
        if status == "healthy":
            return status
        if status in {"dead", "exited", "unhealthy"}:
            return status
        time.sleep(POLL_INTERVAL_SECONDS)
    return status


def _container_status(container_id: str) -> str:
    completed = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container_id,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _read_health() -> dict[str, object]:
    with urlopen(HEALTH_URL, timeout=5.0) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError(f"unexpected health payload: {payload!r}")
    return payload


def _print_diagnostics_hint() -> None:
    print(f"Inspect status with: {_status_command()}", file=sys.stderr)
    print(f"Inspect logs with: {_logs_command()}", file=sys.stderr)


def _status_command() -> str:
    return _display_command("ps")


def _logs_command() -> str:
    return _display_command("logs", "--tail", "100", "mem0", "qdrant")


def _stop_command() -> str:
    return _display_command("stop", "mem0", "qdrant")


def _display_command(*args: str) -> str:
    return (
        "docker compose --env-file .env "
        "-f docker/mem0/compose.yaml --profile mem0 "
        + " ".join(args)
    )


if __name__ == "__main__":
    raise SystemExit(main())
