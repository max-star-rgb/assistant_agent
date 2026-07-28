"""Stable command line entrypoint for task-centered Agent evals."""

from __future__ import annotations

import json
import os
from argparse import ArgumentParser
from pathlib import Path
from typing import Sequence

from langfuse import Langfuse

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.runtime.chat_adapter import create_chat_adapter
from evals.agent.calibration import run_calibration
from evals.agent.judge import ProviderSemanticJudge
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
    args = parser.parse_args(argv)
    task_ids = args.task or load_suite(args.suite or "smoke")
    tasks = [load_task(task_id) for task_id in task_ids]

    if args.inspect:
        print(
            json.dumps(
                {
                    "action": "inspect",
                    "tasks": [
                        {
                            "task": task.model_dump(mode="json"),
                            "environment": load_entrypoint(
                                task.environment
                            )().describe(),
                        }
                        for task in tasks
                    ],
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
        judge = ProviderSemanticJudge(create_chat_adapter(config))
        if args.calibrate:
            results = [
                result.model_dump(mode="json")
                for task in tasks
                for result in run_calibration(task, judge)
            ]
            print(
                json.dumps(
                    {"action": "calibrate", "results": results},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if all(result["matched"] for result in results) else 1

        client = _langfuse_client()
        observer = create_required_trace_observer()
        try:
            result = run_tasks(
                client,
                tasks,
                config=config,
                judge=judge,
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                trace_observer=observer,
            )
        finally:
            if not observer.close(timeout=10.0):
                raise RuntimeError(
                    "Langfuse Runtime trace export did not close cleanly."
                )
        rewards = primary_rewards(result)
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
    return Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=langfuse_host_from_env(os.environ),
    )
