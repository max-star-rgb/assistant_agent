#!/usr/bin/env python3
"""Seed or run the Langfuse-native agent capability experiment."""

# ruff: noqa: E402

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import ArgumentParser
from contextlib import contextmanager, nullcontext
from pathlib import Path
from collections.abc import Iterator
from urllib.parse import urlparse

import httpx
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from langfuse import Langfuse

from evals.cases.langfuse.experiment import (
    DEFAULT_DATASET_NAME,
    DEFAULT_DATASET_SEED,
    load_dataset_seed,
    run_langfuse_agent_experiment,
    seed_langfuse_dataset,
)
from assistant_agent.services.assistant_run_service import load_env_file
from assistant_agent.services.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)


def main() -> int:
    parser = ArgumentParser(
        description="Run the Langfuse-native closed-loop agent capability evaluation."
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--seed-source", type=Path, default=DEFAULT_DATASET_SEED)
    parser.add_argument(
        "--seed-dataset",
        action="store_true",
        help="Explicitly upsert the local bootstrap seed before the run.",
    )
    parser.add_argument(
        "--experiment-name",
        default="agent-capability-closed-loop-scripted-v1",
    )
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Seed the Dataset and exit without creating an Experiment run.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the optional Dataset seed without connecting to Langfuse.",
    )
    args = parser.parse_args()

    seed = load_dataset_seed(args.seed_source)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "dataset_name": seed.dataset_name,
                    "seed_hash": seed.content_hash(),
                    "item_ids": [item.id for item in seed.items],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.no_env_file:
        load_env_file(args.env_file)
    public_key, secret_key = langfuse_credentials_from_env(os.environ)
    if not public_key or not secret_key:
        print(
            json.dumps(
                {
                    "error": "langfuse_credentials_missing",
                    "message": (
                        "Set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY or the "
                        "ASSISTANT_AGENT_LANGFUSE_* aliases."
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    client: Langfuse | None = None
    httpx_client: httpx.Client | None = None
    try:
        host = langfuse_host_from_env(os.environ)
        loopback = _is_loopback_url(host)
        if loopback:
            httpx_client = httpx.Client(trust_env=False)
        proxy_context = _without_proxy_env() if loopback else nullcontext()
        with proxy_context:
            client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=host,
                httpx_client=httpx_client,
            )
        if not client.auth_check():
            print(
                json.dumps(
                    {
                        "error": "langfuse_auth_failed",
                        "message": "Langfuse credentials or service are unavailable.",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        seed_result = None
        if args.seed_dataset or args.seed_only:
            seed_result = seed_langfuse_dataset(client, seed)
        if args.seed_only:
            print(seed_result.model_dump_json(indent=2))
            return 0
        result = run_langfuse_agent_experiment(
            client,
            dataset_name=args.dataset_name,
            experiment_name=args.experiment_name,
            run_name=args.run_name,
            max_concurrency=max(1, args.max_concurrency),
            metadata=_run_metadata(),
        )
        client.flush()
        print(
            json.dumps(
                {
                    "dataset_name": args.dataset_name,
                    "seeded": seed_result is not None,
                    "experiment_name": result.name,
                    "run_name": result.run_name,
                    "experiment_id": result.experiment_id,
                    "dataset_run_id": result.dataset_run_id,
                    "dataset_run_url": result.dataset_run_url,
                    "item_count": len(result.item_results),
                    "scoring": "langfuse_code_evaluator_async",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - operator command must fail explicitly.
        print(
            json.dumps(
                {
                    "error": "langfuse_experiment_failed",
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.flush()
        if httpx_client is not None:
            httpx_client.close()


def _run_metadata() -> dict[str, str]:
    commit = _git_output("rev-parse", "HEAD") or "unknown"
    dirty = bool(_git_output("status", "--short"))
    return {
        "git_commit": commit,
        "dirty_worktree": str(dirty).lower(),
        "execution_strategy": "react",
        "runtime_config_fingerprint": "agent-capability-scripted-mock-v1",
        "tool_catalog_fingerprint": "calendar-read-write-v1",
        "fixture_version": "calendar_capabilities_v1",
    }


def _git_output(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _is_loopback_url(value: str) -> bool:
    return urlparse(value).hostname in {"127.0.0.1", "::1", "localhost"}


@contextmanager
def _without_proxy_env() -> Iterator[None]:
    keys = (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    )
    previous = {key: os.environ[key] for key in keys if key in os.environ}
    for key in keys:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(previous)


if __name__ == "__main__":
    raise SystemExit(main())
