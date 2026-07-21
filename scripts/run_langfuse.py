#!/usr/bin/env python3
"""Run the repository-local Langfuse stack as one PyCharm process."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = REPO_ROOT / ".data" / "langfuse"
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
HEALTH_URL = "http://127.0.0.1:3000/api/public/health"
STARTUP_TIMEOUT_SECONDS = 600.0
STOP_TIMEOUT_SECONDS = 180.0


def main() -> int:
    if not COMPOSE_FILE.is_file():
        print(f"Langfuse compose file not found: {COMPOSE_FILE}", file=sys.stderr)
        return 2

    stop_requested = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        _compose(
            "up",
            "-d",
            timeout=STARTUP_TIMEOUT_SECONDS,
            stop_requested=stop_requested,
        )
        version = _wait_until_healthy(stop_requested)
        if version is None:
            return 130 if stop_requested.is_set() else 1
        print(f"Langfuse ready: http://localhost:3000 (version {version})", flush=True)
        print("Stop this PyCharm run configuration to stop Langfuse.", flush=True)
        stop_requested.wait()
        return 0
    except InterruptedError:
        return 130
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Failed to run Langfuse: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            _compose("stop", "--timeout", "60", timeout=STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"Failed to stop Langfuse cleanly: {exc}", file=sys.stderr)


def _compose(
    *args: str,
    timeout: float | None = None,
    stop_requested: Event | None = None,
) -> None:
    process = subprocess.Popen(
        ["docker", "compose", "--progress", "plain", *args],
        cwd=COMPOSE_DIR,
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


def _wait_until_healthy(stop_requested: Event) -> str | None:
    deadline = monotonic() + STARTUP_TIMEOUT_SECONDS
    last_error = "health endpoint unavailable"
    while monotonic() < deadline and not stop_requested.wait(1.0):
        try:
            with urlopen(HEALTH_URL, timeout=5.0) as response:
                payload = json.load(response)
            if payload.get("status") == "OK":
                return str(payload.get("version") or "unknown")
            last_error = f"unexpected health payload: {payload!r}"
        except (OSError, URLError, ValueError) as exc:
            last_error = str(exc)
    if not stop_requested.is_set():
        print(f"Langfuse did not become healthy: {last_error}", file=sys.stderr)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
