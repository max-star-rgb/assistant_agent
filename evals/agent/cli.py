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
from evals.agent.contracts import TaskSpec
from evals.agent.langfuse_backend import (
    DEFAULT_DATASET_NAME,
    active_dataset_task_ids,
    create_required_trace_observer,
    publish_tasks,
    run_native_calibration,
    run_tasks,
    verify_persisted_dimension_scores,
)
from evals.agent.loader import (
    list_suites,
    list_task_ids,
    load_entrypoint,
    load_case_source,
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
        help="Select a named task suite; defaults to deep_research.",
    )
    selector.add_argument(
        "--dataset-active",
        action="store_true",
        help="Select all ACTIVE Git-owned tasks published in the Dataset.",
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
        help=(
            "Required for Langfuse native Evaluator calibration or Agent runs "
            "using a real Chat Provider."
        ),
    )
    args = parser.parse_args(argv)
    if args.dataset_active and not args.run:
        parser.error("--dataset-active is only supported with --run.")
    tasks: list[TaskSpec] = []
    if not args.dataset_active:
        task_ids = args.task or load_suite(args.suite or "deep_research")
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
    if not args.publish and not args.allow_real_provider:
        parser.error("--calibrate and --run require --allow-real-provider.")

    try:
        client = None
        if args.dataset_active:
            client = _langfuse_client()
            task_ids = active_dataset_task_ids(
                client,
                dataset_name=args.dataset_name,
            )
            if not task_ids:
                raise RuntimeError(
                    "Dataset has no ACTIVE Agent eval tasks."
                )
            tasks = [load_task(task_id) for task_id in task_ids]
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

        if args.calibrate:
            _emit_progress(
                {
                    "event": "agent_eval.calibration.started",
                    "task_count": len(tasks),
                }
            )
            client = client or _langfuse_client()
            _, calibration_results = run_native_calibration(
                client,
                tasks,
                run_name=args.run_name,
                progress=_emit_progress,
            )
            results = [item.model_dump(mode="json") for item in calibration_results]
            print(
                json.dumps(
                    {"action": "calibrate", "results": results},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if all(result["matched"] for result in results) else 1

        config = ProviderConfig.from_env()
        validate_real_chat_config(config)
        client = client or _langfuse_client()
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
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                trace_observer=observer,
                progress=_emit_progress,
                active_only=args.dataset_active,
            )
        finally:
            if not observer.close(timeout=10.0):
                raise RuntimeError(
                    "Langfuse Runtime trace export did not close cleanly."
                )
        dimension_scores = verify_persisted_dimension_scores(client, result)
        _emit_progress(
            {
                "event": "agent_eval.run.completed",
                "run_name": result.run_name,
                "item_count": len(dimension_scores),
            }
        )
        print(
            json.dumps(
                {
                    "action": "run",
                    "run_name": result.run_name,
                    "dataset_run_url": result.dataset_run_url,
                    "dimension_scores": dimension_scores,
                },
                ensure_ascii=False,
            )
        )
        return 0
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
    source = load_case_source(task.id)
    environment = load_entrypoint(task.environment)()
    objective_method = getattr(environment, "objective_state_assertions", None)
    if source.level == "mission" and not callable(objective_method):
        raise RuntimeError(
            f"Mission {task.id!r} must define objective_state_assertions()."
        )
    return {
        "case_source": {
            "level": source.level,
            "path": source.relative_path,
        },
        "mission_objective_rule": {
            "required": source.level == "mission",
            "implemented": callable(objective_method),
        },
        "task": task.model_dump(mode="json"),
        "environment": environment.describe(),
        "environment_validation": environment.validate().model_dump(mode="json"),
        "tool_outcome_expectations": [
            expectation.model_dump(mode="json")
            for expectation in environment.tool_outcome_expectations()
        ],
    }
