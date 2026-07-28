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
from functools import partial
from pathlib import Path
from collections.abc import Collection, Iterable, Iterator
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
    DETERMINISTIC_SCORE_NAMES,
    REAL_READONLY_SEMANTIC_SCORE_NAMES,
    REAL_SYSTEM_SEMANTIC_SCORE_NAMES,
    run_langfuse_agent_experiment,
)
from evals.cases.langfuse.dataset_sync import (
    failed_dataset_item_ids,
    load_dataset_seed,
    partition_available_dataset_item_ids,
    sync_langfuse_dataset,
)
from evals.cases.langfuse.runtime_profiles import (
    build_real_readonly_runtime,
    build_real_system_runtime,
    validate_real_chat_config,
    validate_real_readonly_config,
)
from evals.cases.langfuse.manifest import (
    load_eval_manifest,
    select_eval_item_ids,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)


def main() -> int:
    manifest = load_eval_manifest()
    parser = ArgumentParser(
        description="Run the Langfuse-native closed-loop agent capability evaluation."
    )
    parser.add_argument("--dataset-name", default=None)
    parser.add_argument("--seed-source", type=Path, default=None)
    parser.add_argument(
        "--sync-dataset",
        action="store_true",
        help="Synchronize the complete managed Dataset before the Experiment.",
    )
    parser.add_argument(
        "--seed-dataset",
        action="store_true",
        help="Deprecated compatibility alias for --sync-dataset.",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
    )
    parser.add_argument("--run-name", default=None)
    parser.add_argument(
        "--rerun-failed-from",
        metavar="RUN_NAME",
        default=None,
        type=_optional_run_name,
        help=(
            "Run only Dataset items whose latest native score was explicitly "
            "false in the named Dataset run; use 'none' for a full run."
        ),
    )
    parser.add_argument(
        "--suite",
        choices=sorted(manifest.suites),
        default=None,
        help="Named case selection from the eval manifest.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Run one Dataset item; repeat to select multiple ids.",
    )
    parser.add_argument(
        "--capability",
        action="append",
        choices=sorted(manifest.capabilities),
        default=[],
        help="Run cases with this stable capability; repeat for multiple values.",
    )
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--local-calendar-path",
        type=Path,
        default=Path(".data/evals/langfuse/calendar.sqlite3"),
        help="SQLite calendar used by --real-system instead of Google Calendar MCP.",
    )
    parser.add_argument(
        "--sync-dataset-only",
        action="store_true",
        help="Synchronize the complete managed Dataset and exit.",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Deprecated compatibility alias for --sync-dataset-only.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env-file", action="store_true")
    profile_group = parser.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--profile",
        choices=sorted(manifest.profiles),
        default=None,
        help="Execution environment; independent from case capability.",
    )
    profile_group.add_argument(
        "--real-readonly",
        action="store_true",
        help=(
            "Compatibility alias for --profile real_readonly."
        ),
    )
    profile_group.add_argument(
        "--real-system",
        action="store_true",
        help=(
            "Compatibility alias for --profile real_system."
        ),
    )
    parser.add_argument(
        "--allow-real-tools",
        action="store_true",
        help="Operator confirmation required before any real-provider Experiment.",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="Additional confirmation required when selected cases can write state.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Inspect manifest resolution and selected local cases without network.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Deprecated compatibility alias for --inspect.",
    )
    args = parser.parse_args()
    inspect_only = args.inspect or args.dry_run
    sync_dataset = args.sync_dataset or args.seed_dataset
    sync_dataset_only = args.sync_dataset_only or args.seed_only
    if inspect_only and (sync_dataset or sync_dataset_only):
        parser.error("--inspect cannot be combined with Dataset synchronization.")
    if args.rerun_failed_from and (
        sync_dataset_only
        or inspect_only
        or args.case_id
        or args.capability
        or args.suite
    ):
        parser.error(
            "--rerun-failed-from cannot be combined with seed/dry-run or "
            "explicit suite/case/capability selectors."
        )
    if sync_dataset_only and (args.case_id or args.capability):
        parser.error(
            "--sync-dataset-only always synchronizes the complete Dataset."
        )

    legacy_profile = (
        "real_system"
        if args.real_system
        else "real_readonly"
        if args.real_readonly
        else None
    )
    explicit_profile = args.profile or legacy_profile
    if args.suite:
        execution_profile = (
            explicit_profile or manifest.suites[args.suite].default_profile
        )
    else:
        execution_profile = explicit_profile or "scripted_mock"
    profile = manifest.profiles[execution_profile]
    suite_name = args.suite or profile.default_suite
    suite = manifest.suites[suite_name]
    dataset = manifest.datasets[suite.dataset]
    if dataset.kind == "infrastructure" and execution_profile != "scripted_mock":
        parser.error("Infrastructure suites require profile 'scripted_mock'.")
    if dataset.kind == "behavior" and execution_profile == "scripted_mock":
        parser.error("Behavior suites require a real execution profile.")
    dataset_name = args.dataset_name or dataset.dataset_name
    seed_source = args.seed_source or dataset.seed_source
    experiment_name = args.experiment_name or profile.experiment_name
    seed = load_dataset_seed(seed_source)
    if inspect_only:
        try:
            dry_run_item_ids = select_eval_item_ids(
                seed.items,
                manifest=manifest,
                suite_name=suite_name,
                profile_name=execution_profile,
                case_ids=args.case_id,
                capabilities=args.capability,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "inspect": True,
                    "execution_profile": execution_profile,
                    "suite": suite_name,
                    "dataset_name": seed.dataset_name,
                    "seed_hash": seed.content_hash(),
                    "item_ids": dry_run_item_ids,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.no_env_file:
        load_env_file(args.env_file)
    runtime_factory = None
    runtime_config = None
    if execution_profile != "scripted_mock" and not sync_dataset_only:
        if not args.allow_real_tools:
            print(
                json.dumps(
                    {
                        "error": "real_tools_not_authorized",
                        "message": (
                            "Real-provider Experiment requires "
                            "--allow-real-tools."
                        ),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        runtime_config = ProviderConfig.from_env()
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
        if sync_dataset or sync_dataset_only:
            seed_result = sync_langfuse_dataset(client, seed)
        if sync_dataset_only:
            print(seed_result.model_dump_json(indent=2))
            return 0
        dataset = client.get_dataset(dataset_name)
        selected_item_ids: list[str] | None = None
        skipped_unavailable_item_ids: list[str] = []
        if args.rerun_failed_from:
            failed_item_ids = failed_dataset_item_ids(
                client,
                dataset_name=dataset_name,
                run_name=args.rerun_failed_from,
                score_names=(
                    *DETERMINISTIC_SCORE_NAMES,
                    *(
                        REAL_SYSTEM_SEMANTIC_SCORE_NAMES
                        if execution_profile == "real_system"
                        else REAL_READONLY_SEMANTIC_SCORE_NAMES
                    ),
                ),
            )
            selected_item_ids, skipped_unavailable_item_ids = (
                partition_available_dataset_item_ids(
                    dataset,
                    failed_item_ids,
                )
            )
            if not selected_item_ids:
                print(
                    json.dumps(
                        {
                            "dataset_name": dataset_name,
                            "rerun_failed_from": args.rerun_failed_from,
                            "selected_item_ids": [],
                            "skipped_unavailable_item_ids": (
                                skipped_unavailable_item_ids
                            ),
                            "message": (
                                "No runnable explicitly failed Dataset items found."
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
        else:
            selected_item_ids = select_eval_item_ids(
                dataset.items,
                manifest=manifest,
                suite_name=suite_name,
                profile_name=execution_profile,
                case_ids=args.case_id,
                capabilities=args.capability,
            )
        if runtime_config is not None:
            selected_items = _selected_dataset_items(
                dataset.items,
                selected_item_ids or (),
            )
            if (
                any(
                    "calendar_create"
                    in _item_metadata(item).get("required_tools", [])
                    for item in selected_items
                )
                and not args.allow_writes
            ):
                print(
                    json.dumps(
                        {
                            "error": "writes_not_authorized",
                            "message": (
                                "Selected Dataset items require --allow-writes."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                return 2
            try:
                _validate_real_profile_config(
                    execution_profile,
                    runtime_config,
                    selected_items,
                )
            except RuntimeError as exc:
                print(
                    json.dumps(
                        {
                            "error": "real_provider_not_configured",
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                return 2
            runtime_factory = (
                partial(
                    build_real_system_runtime,
                    config=runtime_config,
                    calendar_path=args.local_calendar_path,
                )
                if execution_profile == "real_system"
                else partial(
                    build_real_readonly_runtime,
                    config=runtime_config,
                )
            )
        result = run_langfuse_agent_experiment(
            client,
            dataset_name=dataset_name,
            experiment_name=experiment_name,
            run_name=args.run_name,
            max_concurrency=max(1, args.max_concurrency),
            metadata=_run_metadata(
                execution_profile=execution_profile,
                suite_name=suite_name,
                config=runtime_config,
            ),
            runtime_factory=runtime_factory,
            execution_profile=execution_profile,
            dataset_item_ids=selected_item_ids,
        )
        client.flush()
        print(
            json.dumps(
                {
                    "dataset_name": dataset_name,
                    "seeded": seed_result is not None,
                    "execution_profile": execution_profile,
                    "suite": suite_name,
                    "experiment_name": result.name,
                    "run_name": result.run_name,
                    "experiment_id": result.experiment_id,
                    "dataset_run_id": result.dataset_run_id,
                    "dataset_run_url": result.dataset_run_url,
                    "item_count": len(result.item_results),
                    "rerun_failed_from": args.rerun_failed_from,
                    "selected_item_ids": selected_item_ids,
                    "skipped_unavailable_item_ids": (
                        skipped_unavailable_item_ids
                    ),
                    "scoring": "langfuse_native_evaluators_async",
                    "expected_score_names": [
                        *DETERMINISTIC_SCORE_NAMES,
                        *(
                            REAL_SYSTEM_SEMANTIC_SCORE_NAMES
                            if execution_profile == "real_system"
                            else REAL_READONLY_SEMANTIC_SCORE_NAMES
                        ),
                    ],
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


def _run_metadata(
    *,
    execution_profile: str,
    suite_name: str,
    config: ProviderConfig | None,
) -> dict[str, str]:
    commit = _git_output("rev-parse", "HEAD") or "unknown"
    dirty = bool(_git_output("status", "--short"))
    provider = config.chat_provider if config is not None else "scripted"
    model = (
        config.chat_model or config.resolved_chat_provider().model or "unknown"
        if config is not None
        else "scripted-calendar-eval"
    )
    return {
        "git_commit": commit,
        "dirty_worktree": str(dirty).lower(),
        "execution_strategy": "react",
        "execution_profile": execution_profile,
        "suite": suite_name,
        "chat_provider": provider,
        "chat_model": model,
        "runtime_config_fingerprint": (
            "agent-behavior-system-v2"
            if execution_profile == "real_system"
            else "agent-behavior-readonly-v2"
            if execution_profile == "real_readonly"
            else "agent-infrastructure-scripted-v1"
        ),
        "tool_catalog_fingerprint": (
            "configured-real-tools-local-calendar-v1"
            if execution_profile == "real_system"
            else "weather-live-and-controlled-failure-v2"
            if execution_profile == "real_readonly"
            else "calendar-read-write-v1"
        ),
        "fixture_version": (
            "real-system-local-calendar-v1"
            if execution_profile == "real_system"
            else "dynamic-readonly-v2"
            if execution_profile == "real_readonly"
            else "calendar_capabilities_v1"
        ),
    }


def _selected_dataset_items(
    items: Iterable[object],
    selected_item_ids: Collection[str],
) -> list[object]:
    selected = set(selected_item_ids)
    return [item for item in items if str(getattr(item, "id", "")) in selected]


def _validate_real_profile_config(
    execution_profile: str,
    config: ProviderConfig,
    selected_items: list[object],
) -> None:
    if selected_items and all(
        _item_metadata(item).get("capability")
        == "tool_failure_recovery"
        for item in selected_items
    ):
        validate_real_chat_config(config)
        return
    if execution_profile == "real_system":
        needs_live_weather = any(
            "weather" in _item_metadata(item).get("required_tools", [])
            and _item_metadata(item).get("dependency_mode") != "simulated"
            for item in selected_items
        )
        if needs_live_weather:
            validate_real_readonly_config(config)
        else:
            validate_real_chat_config(config)
        return
    needs_live_weather = any(
        "weather" in _item_metadata(item).get("required_tools", [])
        and _item_metadata(item).get("dependency_mode") != "simulated"
        for item in selected_items
    )
    if needs_live_weather:
        validate_real_readonly_config(config)
    else:
        validate_real_chat_config(config)


def _item_metadata(item: object) -> dict[str, object]:
    metadata = getattr(item, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _optional_run_name(value: str) -> str | None:
    normalized = value.strip()
    return None if normalized.lower() == "none" else normalized


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
