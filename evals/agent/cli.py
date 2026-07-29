"""Stable command line entrypoint for task-centered Agent evals."""

from __future__ import annotations

import json
import os
from argparse import ArgumentParser
from pathlib import Path
import sys
from typing import Sequence

from langfuse import Langfuse

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.providers.provider_http import (
    without_unsupported_socks_proxy_env,
)
from assistant_agent.runtime.assistant_run_service import load_env_file
from evals.agent.calibration import run_calibration
from evals.agent.contracts import TaskSpec
from evals.agent.judge import (
    JUDGE_MAX_RETRIES_ENV,
    JUDGE_NETWORK_MODES,
    JUDGE_NETWORK_MODE_ENV,
    JUDGE_TIMEOUT_ENV,
    create_provider_judge,
)
from evals.agent.langfuse_backend import (
    DEFAULT_DATASET_NAME,
    create_required_trace_observer,
    primary_rewards,
    publish_tasks,
    run_tasks,
)
from evals.agent.loader import (
    list_suites,
    list_task_ids,
    load_entrypoint,
    load_suite,
    load_task,
)
from evals.agent.provider_gate import validate_real_chat_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        description="Run Git-owned Agent eval tasks with Langfuse as backend."
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--task",
        action="append",
        choices=list_task_ids(),
        help="Select one task; repeat to select multiple tasks.",
    )
    selector.add_argument(
        "--suite",
        choices=list_suites(),
        help="Select a named task suite; defaults to smoke.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--calibrate", action="store_true")
    action.add_argument("--publish", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--allow-real-provider",
        action="store_true",
        help="Required for calibration or Agent runs using a real Chat Provider.",
    )
    parser.add_argument(
        "--judge-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Override the LLM Judge timeout; defaults to "
            f"{JUDGE_TIMEOUT_ENV} or 30 seconds."
        ),
    )
    parser.add_argument(
        "--judge-max-retries",
        type=int,
        default=None,
        help=(
            "Override LLM Judge SDK retries; defaults to "
            f"{JUDGE_MAX_RETRIES_ENV} or 0."
        ),
    )
    parser.add_argument(
        "--judge-network-mode",
        choices=JUDGE_NETWORK_MODES,
        default=None,
        help=(
            "Select Judge networking: environment honors proxy settings; "
            "ipv4_direct bypasses proxies and forces IPv4. Defaults to "
            f"{JUDGE_NETWORK_MODE_ENV} or ipv4_direct."
        ),
    )
    args = parser.parse_args(argv)
    task_ids = args.task or load_suite(args.suite or "smoke")
    tasks = [load_task(task_id) for task_id in task_ids]

    if args.inspect:
        print(
            json.dumps(
                {
                    "action": "inspect",
                    "tasks": [_inspect_task(task) for task in tasks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.no_env_file:
        load_env_file(args.env_file)

    try:
        if args.publish:
            client = _langfuse_client()
            item_ids = publish_tasks(
                client,
                tasks,
                dataset_name=args.dataset_name,
            )
            client.flush()
            print(
                json.dumps(
                    {
                        "action": "publish",
                        "dataset_name": args.dataset_name,
                        "item_ids": item_ids,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if not args.allow_real_provider:
            parser.error("--calibrate and --run require --allow-real-provider.")
        config = ProviderConfig.from_env()
        validate_real_chat_config(config)
        judge_env = dict(os.environ)
        if args.judge_timeout_seconds is not None:
            judge_env[JUDGE_TIMEOUT_ENV] = str(args.judge_timeout_seconds)
        if args.judge_max_retries is not None:
            judge_env[JUDGE_MAX_RETRIES_ENV] = str(args.judge_max_retries)
        if args.judge_network_mode is not None:
            judge_env[JUDGE_NETWORK_MODE_ENV] = args.judge_network_mode
        if args.calibrate:
            _emit_progress(
                {
                    "event": "agent_eval.calibration.started",
                    "task_count": len(tasks),
                }
            )
            judge = create_provider_judge(
                config,
                env=judge_env,
                progress=_emit_progress,
            )
            try:
                results = [
                    result.model_dump(mode="json")
                    for task in tasks
                    for result in run_calibration(task, judge)
                ]
            finally:
                judge.close()
            _emit_progress(
                {
                    "event": "agent_eval.calibration.completed",
                    "fixture_count": len(results),
                }
            )
            print(
                json.dumps(
                    {"action": "calibrate", "results": results},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if all(result["matched"] for result in results) else 1

        client = _langfuse_client()
        judge = create_provider_judge(
            config,
            env=judge_env,
            langfuse=client,
            progress=_emit_progress,
        )
        observer = create_required_trace_observer()
        _emit_progress(
            {
                "event": "agent_eval.run.started",
                "run_name": args.run_name,
                "task_count": len(tasks),
            }
        )
        try:
            result = run_tasks(
                client,
                tasks,
                config=config,
                judge=judge,
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                trace_observer=observer,
                progress=_emit_progress,
            )
        finally:
            judge.close()
            if not observer.close(timeout=10.0):
                raise RuntimeError(
                    "Langfuse Runtime trace export did not close cleanly."
                )
        rewards = primary_rewards(result)
        _emit_progress(
            {
                "event": "agent_eval.run.completed",
                "run_name": result.run_name,
                "reward_count": len(rewards),
            }
        )
        print(
            json.dumps(
                {
                    "action": "run",
                    "run_name": result.run_name,
                    "dataset_run_url": result.dataset_run_url,
                    "rewards": rewards,
                },
                ensure_ascii=False,
            )
        )
        return 0 if rewards and all(reward == 1.0 for reward in rewards) else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": "agent_eval_infrastructure_failure",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2


def _langfuse_client() -> Langfuse:
    public_key, secret_key = langfuse_credentials_from_env(os.environ)
    if not public_key or not secret_key:
        raise RuntimeError("Langfuse credentials are required.")
    with without_unsupported_socks_proxy_env():
        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=langfuse_host_from_env(os.environ),
        )


def _emit_progress(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False),
        file=sys.stderr,
        flush=True,
    )


def _inspect_task(task: TaskSpec) -> dict[str, object]:
    environment = load_entrypoint(task.environment)()
    return {
        "task": task.model_dump(mode="json"),
        "environment": environment.describe(),
        "environment_validation": environment.validate().model_dump(mode="json"),
        "tool_outcome_expectations": [
            expectation.model_dump(mode="json")
            for expectation in environment.tool_outcome_expectations()
        ],
    }
