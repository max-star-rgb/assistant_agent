#!/usr/bin/env python3
"""Run the repository-local Qdrant service as one PyCharm process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import monotonic
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker" / "mem0" / "compose.yaml"
HEALTH_URL = "http://127.0.0.1:6333/healthz"
STARTUP_TIMEOUT_SECONDS = 600.0
STOP_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 5.0

ComposeFn = Callable[..., None]
HealthFn = Callable[[Event], bool]
ProbeFn = Callable[[], bool]


def main() -> int:
    if not COMPOSE_FILE.is_file():
        print(f"Qdrant compose file not found: {COMPOSE_FILE}", file=sys.stderr)
        return 2

    stop_requested = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return _supervise_qdrant(stop_requested=stop_requested)


def _supervise_qdrant(
    *,
    stop_requested: Event,
    compose_fn: ComposeFn | None = None,
    health_fn: HealthFn | None = None,
) -> int:
    compose = compose_fn or _compose
    wait_until_healthy = health_fn or _wait_until_healthy
    start_attempted = False
    result = 1
    try:
        start_attempted = True
        compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "qdrant",
            timeout=STARTUP_TIMEOUT_SECONDS,
            stop_requested=stop_requested,
        )
        if not wait_until_healthy(stop_requested):
            return 130 if stop_requested.is_set() else 1
        print(f"Qdrant ready: {HEALTH_URL}", flush=True)
        print("Stop this PyCharm run configuration to stop Qdrant.", flush=True)
        stop_requested.wait()
        result = 0
    except InterruptedError:
        result = 130
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Failed to run Qdrant: {exc}", file=sys.stderr)
        result = 1
    finally:
        if start_attempted:
            try:
                compose("stop", "qdrant", timeout=STOP_TIMEOUT_SECONDS)
            except (
                OSError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ) as exc:
                print(f"Failed to stop Qdrant cleanly: {exc}", file=sys.stderr)
                result = 1
    return result


def _compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "visual-memory",
        *args,
    ]


def _compose(
    *args: str,
    timeout: float | None = None,
    stop_requested: Event | None = None,
) -> None:
    process = subprocess.Popen(
        _compose_command(*args),
        cwd=REPO_ROOT,
        start_new_session=True,
    )
    deadline = None if timeout is None else monotonic() + timeout
    while True:
        try:
            return_code = process.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if stop_requested is not None and stop_requested.is_set():
                _terminate_process_group(process)
                raise InterruptedError
            if deadline is not None and monotonic() >= deadline:
                _terminate_process_group(process)
                raise subprocess.TimeoutExpired(process.args, timeout)
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, process.args)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5.0)


def _wait_until_healthy(
    stop_requested: Event,
    *,
    health_probe: ProbeFn | None = None,
    service_running: ProbeFn | None = None,
) -> bool:
    probe = health_probe or _probe_health
    is_running = service_running or _qdrant_is_running
    deadline = monotonic() + STARTUP_TIMEOUT_SECONDS
    last_error = "health endpoint unavailable"
    while monotonic() < deadline and not stop_requested.is_set():
        try:
            if probe():
                return True
        except (OSError, URLError) as exc:
            last_error = str(exc)
        if not is_running():
            print("Qdrant exited before becoming healthy.", file=sys.stderr)
            print(
                "Inspect logs with: "
                + " ".join(_compose_command("logs", "--tail", "100", "qdrant")),
                file=sys.stderr,
            )
            print(
                "If logs mention RocksDB, upgrade persistent storage one minor "
                "version at a time: 1.15 -> 1.16 -> 1.17 -> 1.18.",
                file=sys.stderr,
            )
            return False
        stop_requested.wait(POLL_INTERVAL_SECONDS)
    if not stop_requested.is_set():
        print(f"Qdrant did not become healthy: {last_error}", file=sys.stderr)
    return False


def _probe_health() -> bool:
    with urlopen(HEALTH_URL, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.status == 200


def _qdrant_is_running() -> bool:
    completed = subprocess.run(
        _compose_command("ps", "--status", "running", "-q", "qdrant"),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


if __name__ == "__main__":
    raise SystemExit(main())
