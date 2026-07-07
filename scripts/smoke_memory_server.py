#!/usr/bin/env python3
# ruff: noqa: E402
"""Manual smoke entry point for external Memory Server retrieval."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.memory.remote import RemoteMemoryClient
from assistant_agent.schemas.memory import MemoryQuery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an opt-in Memory Server health and query smoke test.",
    )
    parser.add_argument("--base-url", default=None, help="Memory Server base URL, e.g. http://127.0.0.1:5200.")
    parser.add_argument("--user-id", required=True, help="User id to use for the scoped health and query calls.")
    parser.add_argument("--session-id", default=None, help="Optional session id for health and query calls.")
    parser.add_argument("--query", required=True, help="Natural language memory query.")
    parser.add_argument("--top-k", type=int, default=3, help="Maximum text memories to retrieve.")
    parser.add_argument("--timeout-seconds", type=float, default=2.0, help="HTTP timeout for each Memory Server call.")
    parser.add_argument("--strategy", default="vector", help="Memory Server retrieval strategy.")
    parser.add_argument("--trace", action="store_true", help="Request Memory Server trace metadata.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = os.environ if env is None else env
    base_url = args.base_url or source.get("MEMORY_SERVER_BASE_URL")
    if not base_url:
        _print_json(
            {
                "status": "provider_unconfigured",
                "capability": "memory_server",
                "error": "missing MEMORY_SERVER_BASE_URL",
            }
        )
        return 2

    client = RemoteMemoryClient(
        base_url=base_url,
        timeout_seconds=args.timeout_seconds,
        query_strategy=args.strategy,
        direct_answer=False,
        include_media_chunks=False,
        trace=args.trace,
    )
    health = client.health(user_id=args.user_id, session_id=args.session_id)
    query = MemoryQuery(
        user_id=args.user_id,
        session_id=args.session_id,
        query=args.query,
        top_k=args.top_k,
    )
    result = client.query_memories(query)
    output = {
        "status": "success" if not result.errors else "failed",
        "capability": "memory_server",
        "base_url": base_url,
        "health_status": health.get("status", ""),
        "health_version": health.get("version", ""),
        "query": args.query,
        "user_id": args.user_id,
        "session_id": args.session_id,
        "strategy": args.strategy,
        "direct_answer": False,
        "include_media_chunks": False,
        "result_count": len(result.items),
        "memory_ids": [item.memory_id for item in result.items],
        "summaries": [item.summary for item in result.items],
        "errors": result.errors,
    }
    _print_json(output)
    return 0 if not result.errors else 1


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
