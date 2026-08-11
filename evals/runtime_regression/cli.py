from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from langfuse import Langfuse

from assistant_agent.config import ProviderConfig
from assistant_agent.evaluation.experiment_runtime import (
    create_experiment_runtime_host,
)
from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.providers.provider_http import without_unsupported_socks_proxy_env
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.runtime.runtime import AgentGraphRuntime

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from .experiment import (
    RuntimeRegressionExperimentSettings,
    inspect_runtime_regression_dataset,
    run_runtime_regression_experiment,
    wait_for_runtime_regression_scores,
    wait_for_runtime_regression_trace_completeness,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Langfuse-owned cases through the production runtime."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--score-wait-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--allow-runtime-side-effects", action="store_true")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)

    if not args.no_env_file:
        load_env_file(args.env_file, override=False)
    client = _langfuse_client()
    try:
        if args.inspect:
            _, active = inspect_runtime_regression_dataset(client)
            print(
                json.dumps(
                    {
                        "action": "inspect",
                        "dataset_name": RUNTIME_REGRESSION_DATASET,
                        "active_item_count": len(active),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if not args.allow_real_provider:
            action_name = "preflight" if args.preflight else "run"
            parser.error(f"--{action_name} requires --allow-real-provider")
        if not args.allow_runtime_side_effects:
            parser.error(
                f"--{'preflight' if args.preflight else 'run'} requires "
                "--allow-runtime-side-effects"
            )
        if args.max_concurrency < 1:
            parser.error("--max-concurrency must be positive")
        config = ProviderConfig.from_env()
        if config.provider_mode != "real":
            raise RuntimeError(
                "runtime regression Experiment requires MULTIMODAL_AGENT_PROVIDER_MODE=real"
            )
        config.validate_provider_mode()
        if args.preflight:
            _, active = inspect_runtime_regression_dataset(client)
            print(
                json.dumps(
                    {
                        "action": "preflight",
                        "status": "ready",
                        "dataset_name": RUNTIME_REGRESSION_DATASET,
                        "active_item_count": len(active),
                        "model": config.resolved_chat_provider().model,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        _require_args(parser, args, "run_name")
        result = run_runtime_regression_experiment(
            client,
            RuntimeRegressionExperimentSettings(
                model=config.resolved_chat_provider().model,
                runtime_factory=lambda: _create_item_runtime(config),
                run_name=args.run_name,
                max_concurrency=args.max_concurrency,
            ),
        )
        client.flush()
        if not result.dataset_run_id:
            raise RuntimeError("Langfuse Experiment returned no id")
        item_scores = wait_for_runtime_regression_scores(
            client,
            experiment_id=result.dataset_run_id,
            dataset_item_ids=result.dataset_item_ids,
            timeout_seconds=args.score_wait_timeout_seconds,
        )
        wait_for_runtime_regression_trace_completeness(
            client,
            experiment_id=result.dataset_run_id,
            dataset_item_ids=result.dataset_item_ids,
        )
        print(
            json.dumps(
                {
                    "action": "run",
                    "dataset_name": RUNTIME_REGRESSION_DATASET,
                    "run_name": result.run_name,
                    "dataset_run_id": result.dataset_run_id,
                    "dataset_run_url": result.dataset_run_url,
                    "dataset_item_ids": list(result.dataset_item_ids),
                    "item_scores": item_scores,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": "runtime_regression_infrastructure_failure",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    finally:
        client.shutdown()


def _langfuse_client() -> Langfuse:
    public_key, secret_key = langfuse_credentials_from_env(os.environ)
    if not public_key or not secret_key:
        raise RuntimeError("Langfuse credentials are required")
    with without_unsupported_socks_proxy_env():
        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=langfuse_host_from_env(os.environ),
        )


def _create_item_runtime(config: ProviderConfig):
    return create_experiment_runtime_host(
        lambda trace_store: AgentGraphRuntime(
            config=config,
            trace_store=trace_store,
        )
    )


def _require_args(parser: argparse.ArgumentParser, args: argparse.Namespace, *names: str) -> None:
    missing = [f"--{name.replace('_', '-')}" for name in names if not getattr(args, name)]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))
