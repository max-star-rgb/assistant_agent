#!/usr/bin/env python3
"""Start the repository's native LangGraph Agent Server deployment."""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
LANGGRAPH = Path(sys.executable).with_name("langgraph")
POSTGRES_COMPOSE_FILE = REPO_ROOT / "deploy" / "agent_server" / "compose.yaml"
POSTGRES_IMAGE = "assistant-agent/langgraph-api:local"
DOCKER_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
DEV_SERVER_LOCK = REPO_ROOT / ".data" / "run" / "agent_server-dev.lock"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the native Agent Server.")
    parser.add_argument(
        "--backend",
        choices=("dev", "postgres"),
        default="dev",
        help="Use local dev persistence or the dedicated PostgreSQL deployment.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
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
        "--rebuild",
        action="store_true",
        help="Rebuild the local Agent Server image before starting postgres backend.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help=(
            "Append combined stdout/stderr to this path while preserving console "
            "output. Dev defaults outside the watched repository; postgres "
            "defaults to .data/logs/agent_server-<port>.log."
        ),
    )
    return parser


def resolve_log_path(log_file: str | None, *, port: int, backend: str) -> Path:
    """Resolve a log path without feeding dev output back into its file watcher."""

    if log_file is None and backend == "dev":
        return (
            Path(tempfile.gettempdir())
            / "assistant_agent"
            / "logs"
            / f"agent_server-{port}.log"
        )
    path = Path(log_file or f".data/logs/agent_server-{port}.log")
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


@contextmanager
def hold_dev_server_lock(lock_path: Path = DEV_SERVER_LOCK) -> Iterator[None]:
    """Allow only one dev server to own the working tree's local persistence."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(
                "another dev server is already running for this working tree"
            ) from exc
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
    env_file: str,
    use_env_file: bool,
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
    if use_env_file:
        env["LANGGRAPH_ENV"] = str((REPO_ROOT / env_file).resolve())
    return env


def _postgres_image_exists() -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", POSTGRES_IMAGE],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _run_postgres_backend(
    args: argparse.Namespace,
    *,
    env: dict[str, str],
    log_path: Path,
) -> int:
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit(
            "postgres backend only binds to loopback; use --host 127.0.0.1"
        )

    env["ASSISTANT_AGENT_SERVER_PORT"] = str(args.port)
    env["ASSISTANT_AGENT_ENV_FILE"] = (
        "/dev/null"
        if args.no_env_file
        else str((REPO_ROOT / args.env_file).resolve())
    )

    image_exists = _postgres_image_exists()
    if args.rebuild or not image_exists:
        build_command = [
            str(LANGGRAPH),
            "build",
            "-t",
            POSTGRES_IMAGE,
            "--network",
            "host",
        ]
        if image_exists:
            build_command.append("--no-pull")
        for variable in DOCKER_PROXY_VARIABLES:
            if env.get(variable):
                build_command.extend(("--build-arg", variable))
        build_result = run_command_with_log(
            build_command,
            cwd=REPO_ROOT,
            env=env,
            log_path=log_path,
        )
        if build_result != 0:
            return build_result

    return run_command_with_log(
        [
            "docker",
            "compose",
            "-f",
            str(POSTGRES_COMPOSE_FILE),
            "up",
            "--remove-orphans",
        ],
        cwd=REPO_ROOT,
        env=env,
        log_path=log_path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = build_server_env(
        os.environ.copy(),
        env_file=args.env_file,
        use_env_file=not args.no_env_file,
    )
    log_path = resolve_log_path(
        args.log_file,
        port=args.port,
        backend=args.backend,
    )
    if args.backend == "postgres":
        return _run_postgres_backend(args, env=env, log_path=log_path)

    require_dev_log_outside_repo(log_path)
    command = [
        str(LANGGRAPH),
        "dev",
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
        require_available_port(args.host, args.port)
        return run_command_with_log(
            command,
            cwd=REPO_ROOT,
            env=env,
            log_path=log_path,
        )


if __name__ == "__main__":
    raise SystemExit(main())
