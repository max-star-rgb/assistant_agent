#!/usr/bin/env python3
"""Start the repository's native LangGraph Agent Server deployment."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ.copy()
    if not args.no_env_file:
        env["LANGGRAPH_ENV"] = str((REPO_ROOT / args.env_file).resolve())
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
    try:
        return subprocess.run(command, cwd=REPO_ROOT, env=env, check=False).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
