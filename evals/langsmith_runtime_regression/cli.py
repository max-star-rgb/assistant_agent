"""Controlled CLI for LangSmith-owned production Runtime regressions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Sequence

from assistant_agent.config import ProviderConfig
from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from assistant_agent.evaluation.experiment_runtime import (
    create_experiment_runtime_host,
)
from assistant_agent.evaluation.langsmith_trace import LangSmithExperimentBinding
from assistant_agent.observability.langsmith_config import (
    create_langsmith_client_from_env,
)
from assistant_agent.observability.otel_exporter import (
    create_langsmith_text_otel_trace_observer_from_env,
)
from assistant_agent.observability.trace_persistence import (
    create_langsmith_experiment_trace_store,
)
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.runtime.runtime import AgentGraphRuntime

from .experiment import (
    LangSmithRuntimeRegressionSettings,
    inspect_langsmith_runtime_regression_dataset,
    run_langsmith_runtime_regression_experiment,
    wait_for_langsmith_runtime_regression_completeness,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run LangSmith-owned cases through the production runtime."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--feedback-wait-timeout-seconds",
        type=float,
        default=180.0,
    )
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--allow-runtime-side-effects", action="store_true")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)

    if not args.no_env_file:
        load_env_file(args.env_file, override=False)
    client = _langsmith_client()
    try:
        if args.inspect:
            _, active = inspect_langsmith_runtime_regression_dataset(client)
            _print_json(
                {
                    "action": "inspect",
                    "backend": "langsmith",
                    "dataset_name": RUNTIME_REGRESSION_DATASET,
                    "active_example_count": len(active),
                }
            )
            return 0
        action_name = "preflight" if args.preflight else "run"
        if not args.allow_real_provider:
            parser.error(f"--{action_name} requires --allow-real-provider")
        if not args.allow_runtime_side_effects:
            parser.error(
                f"--{action_name} requires --allow-runtime-side-effects"
            )
        if args.max_concurrency < 1:
            parser.error("--max-concurrency must be positive")
        config = ProviderConfig.from_env()
        if config.provider_mode != "real":
            raise RuntimeError(
                "runtime regression Experiment requires "
                "MULTIMODAL_AGENT_PROVIDER_MODE=real"
            )
        config.validate_provider_mode()
        if args.preflight:
            _, active = inspect_langsmith_runtime_regression_dataset(client)
            _validate_langsmith_exporter()
            _print_json(
                {
                    "action": "preflight",
                    "backend": "langsmith",
                    "status": "ready",
                    "dataset_name": RUNTIME_REGRESSION_DATASET,
                    "active_example_count": len(active),
                    "model": config.resolved_chat_provider().model,
                }
            )
            return 0

        _require_args(parser, args, "run_name")
        result = run_langsmith_runtime_regression_experiment(
            client,
            LangSmithRuntimeRegressionSettings(
                model=config.resolved_chat_provider().model,
                runtime_factory=lambda binding: _create_item_runtime(
                    config,
                    binding,
                ),
                run_name=args.run_name,
                git_commit=_git_commit(),
                max_concurrency=args.max_concurrency,
            ),
        )
        client.flush()
        completeness = wait_for_langsmith_runtime_regression_completeness(
            client,
            experiment_id=result.experiment_id,
            example_ids=result.example_ids,
            timeout_seconds=args.feedback_wait_timeout_seconds,
        )
        _print_json(
            {
                "action": "run",
                "backend": "langsmith",
                "dataset_name": RUNTIME_REGRESSION_DATASET,
                "experiment_id": result.experiment_id,
                "experiment_name": result.experiment_name,
                "experiment_url": result.experiment_url,
                "example_ids": list(result.example_ids),
                "run_ids": list(completeness.run_ids),
                "feedback": completeness.feedback,
            }
        )
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        _print_json(
            {
                "error": "langsmith_runtime_regression_infrastructure_failure",
                "message": str(exc),
            }
        )
        return 2
    finally:
        try:
            client.flush()
        finally:
            client.close()


def _langsmith_client():
    return create_langsmith_client_from_env()


def _create_item_runtime(
    config: ProviderConfig,
    binding: LangSmithExperimentBinding,
):
    return create_experiment_runtime_host(
        lambda trace_store: AgentGraphRuntime(
            config=config,
            trace_store=trace_store,
        ),
        trace_store_factory=lambda: create_langsmith_experiment_trace_store(
            project_id=binding.project_id,
        ),
        trace_context_provider=lambda: binding.trace_context,
    )


def _validate_langsmith_exporter() -> None:
    observer = create_langsmith_text_otel_trace_observer_from_env(required=True)
    if observer is None:
        raise RuntimeError("LangSmith trace export is unavailable")
    if observer.close() is False:
        raise RuntimeError("LangSmith trace exporter failed to close")


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if not value:
        raise RuntimeError("git commit identity is unavailable")
    return value


def _require_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *names: str,
) -> None:
    missing = [
        f"--{name.replace('_', '-')}" for name in names if not getattr(args, name)
    ]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))


def _print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False))
