#!/usr/bin/env python3
"""Start the repository's native LangGraph Agent Server deployment."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
LANGGRAPH = Path(sys.executable).with_name("langgraph")
DEV_SERVER_LOCK = REPO_ROOT / "data" / "run" / "agent_server-dev.lock"
LANGGRAPH_CONFIG = REPO_ROOT / "langgraph.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the native Agent Server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument(
        "--config",
        type=Path,
        default=LANGGRAPH_CONFIG,
        help="LangGraph config used by the dev backend.",
    )
    parser.add_argument("--no-reload", action="store_true")
    parser.add_argument("--no-browser", action="store_true", default=True)
    parser.add_argument(
        "--n-jobs-per-worker",
        type=int,
        default=10,
        help=(
            "Maximum concurrent Agent Server jobs for the dev backend. "
            "Keep this above 1 so delayed Memory extraction cannot starve chat runs."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--log-file",
        default=None,
        help=(
            "Append combined stdout/stderr to this path while preserving console "
            "output. Defaults outside the watched repository."
        ),
    )
    return parser


def resolve_log_path(log_file: str | None, *, port: int) -> Path:
    """Resolve a log path without feeding dev output back into its file watcher."""

    if log_file is None:
        return (
            Path(tempfile.gettempdir())
            / "assistant_agent"
            / "logs"
            / f"agent_server-{port}.log"
        )
    path = Path(log_file)
    return path if path.is_absolute() else REPO_ROOT / path


def require_dev_log_outside_repo(log_path: Path) -> None:
    """Prevent hot-reload output from becoming its own filesystem event."""

    if log_path.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise SystemExit(
            "dev log file must be outside the watched repository to avoid a "
            "hot-reload feedback loop"
        )


def require_available_port(host: str, port: int) -> None:
    """Fail before LangGraph can silently fall back to a random port."""

    try:
        with socket.create_server((host, port)):
            pass
    except OSError as exc:
        raise SystemExit(f"port {port} is already in use on {host}") from exc


def release_port(host: str, port: int, *, timeout: float = 5.0) -> None:
    """Stop TCP listeners on the requested port before starting the dev server."""

    try:
        with socket.create_server((host, port)):
            return
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise SystemExit(f"cannot bind {host}:{port}: {exc}") from exc

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            result = subprocess.run(
                ["lsof", "-t", "-a", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"cannot inspect port {port}; lsof is required: {exc}") from exc
        if result.returncode not in (0, 1):
            raise SystemExit(f"cannot inspect port {port}: {result.stderr.strip()}")
        for pid in sorted({int(value) for value in result.stdout.split()}):
            if pid == os.getpid():
                raise SystemExit(f"refusing to terminate this launcher on port {port}")
            print(f"Releasing port {port}: {sig.name} -> PID {pid}", flush=True)
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise SystemExit(f"cannot terminate PID {pid} on port {port}: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                require_available_port(host, port)
                return
            except SystemExit:
                time.sleep(0.1)
    require_available_port(host, port)


def stop_verified_lock_owner(lock_path: Path) -> None:
    """Interrupt only the launcher that owns this working tree's kernel lock."""
    result = subprocess.run(
        ["lslocks", "--json", "--notruncate", "--output", "PID,PATH"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    owners = [item["pid"] for item in json.loads(result.stdout).get("locks", [])
              if item.get("path") == str(lock_path.resolve())]
    if len(owners) != 1:
        raise SystemExit("cannot safely stop lock owner: no unique kernel lock holder")
    pid = int(owners[0])
    try:
        cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        script = (cwd / os.fsdecode(args[1])).resolve() if len(args) > 1 else None
        if pid == os.getpid() or cwd != REPO_ROOT.resolve() or script != (REPO_ROOT / "scripts/run_server.py").resolve():
            raise SystemExit("cannot safely stop lock owner: not this working tree's launcher")
        print(f"Stopping previous Agent Server launcher: SIGINT -> PID {pid}", flush=True)
        # The launcher's KeyboardInterrupt handler terminates and waits for its child.
        os.kill(pid, signal.SIGINT)
    except (FileNotFoundError, ProcessLookupError):
        return


@contextmanager
def hold_dev_server_lock(
    lock_path: Path = DEV_SERVER_LOCK, *, grace_seconds: float = 5.0,
) -> Iterator[None]:
    """Allow only one Agent Server launcher per working tree."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + grace_seconds
        interrupted_owner = False
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    if not interrupted_owner:
                        stop_verified_lock_owner(lock_path)
                        interrupted_owner = True
                        deadline = time.monotonic() + 25
                        continue
                    raise SystemExit(
                        "another Agent Server launcher is still running for this working tree; "
                        "stop the existing run_server.py process before starting from PyCharm"
                    ) from exc
                # The old wrapper releases its lock after its child's output closes.
                time.sleep(0.1)
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def run_command_with_log(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> int:
    """Run one command while teeing its combined output to a durable UTF-8 log."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        try:
            _copy_output(process.stdout, log_file)
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=20)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.kill()
                process.wait()
            return 130


def _copy_output(source: TextIO | None, log_file: TextIO) -> None:
    if source is None:
        return
    for line in source:
        sys.stdout.write(line)
        sys.stdout.flush()
        log_file.write(line)
        log_file.flush()


def build_server_env(
    base_env: dict[str, str],
    *,
    server_port: int,
) -> dict[str, str]:
    """Build a CLI-safe environment without relying on IDE source-root injection."""

    env = dict(base_env)
    source_root = str(REPO_ROOT / "src")
    existing_paths = [
        path for path in env.get("PYTHONPATH", "").split(os.pathsep) if path
    ]
    env["PYTHONPATH"] = os.pathsep.join(
        [source_root, *[path for path in existing_paths if path != source_root]]
    )
    env["ASSISTANT_AGENT_SERVER_PORT"] = str(server_port)
    return env


@contextmanager
def materialize_dev_config(
    *,
    source_config: Path = LANGGRAPH_CONFIG,
    env_file: str,
    use_env_file: bool,
) -> Iterator[Path]:
    """Create a dev config whose env source matches the wrapper CLI flags."""

    config = json.loads(source_config.read_text(encoding="utf-8"))
    config["env"] = (
        str((REPO_ROOT / env_file).resolve()) if use_env_file else {}
    )
    with tempfile.TemporaryDirectory(prefix="assistant-agent-langgraph-") as tmp_dir:
        config_path = Path(tmp_dir) / "langgraph.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        yield config_path


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_config = (
        args.config if args.config.is_absolute() else REPO_ROOT / args.config
    )
    env = build_server_env(
        os.environ.copy(),
        server_port=args.port,
    )
    log_path = resolve_log_path(
        args.log_file,
        port=args.port,
    )
    require_dev_log_outside_repo(log_path)
    with materialize_dev_config(
        source_config=source_config,
        env_file=args.env_file,
        use_env_file=not args.no_env_file,
    ) as config_path:
        command = [
            str(LANGGRAPH),
            "dev",
            "--config",
            str(config_path),
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--no-browser",
            "--n-jobs-per-worker",
            str(args.n_jobs_per_worker),
        ]
        if args.no_reload:
            command.append("--no-reload")
        with hold_dev_server_lock():
            release_port(args.host, args.port)
            require_available_port(args.host, args.port)
            return run_command_with_log(
                command,
                cwd=REPO_ROOT,
                env=env,
                log_path=log_path,
            )


if __name__ == "__main__":
    raise SystemExit(main())
