#!/usr/bin/env python3
"""Start the repository's native LangGraph Agent Server deployment."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
LANGGRAPH = Path(sys.executable).with_name("langgraph")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the native Agent Server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-reload", action="store_true")
    parser.add_argument("--no-browser", action="store_true", default=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--log-file",
        default=".data/logs/agent_server.log",
        help="Append combined Agent Server stdout/stderr while preserving console output.",
    )
    return parser


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = build_server_env(
        os.environ.copy(),
        env_file=args.env_file,
        use_env_file=not args.no_env_file,
    )
    command = [
        str(LANGGRAPH),
        "dev",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--no-browser",
    ]
    if args.no_reload:
        command.append("--no-reload")
    log_path = Path(args.log_file)
    if not log_path.is_absolute():
        log_path = REPO_ROOT / log_path
    return run_command_with_log(
        command,
        cwd=REPO_ROOT,
        env=env,
        log_path=log_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
