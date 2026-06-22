#!/usr/bin/env python3
"""Start the local FastAPI Web demo from a normal Python script.

This wrapper is intentionally thin. It exists so IDEs such as PyCharm can run
the Web UI without requiring a module-based uvicorn run configuration.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local Assistant Web demo.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload for local development.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import uvicorn

    url = f"http://{args.host}:{args.port}/demo/console"
    print(f"Starting Assistant Web demo: {url}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(
        "multimodal_agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
