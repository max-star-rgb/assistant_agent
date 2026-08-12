"""Controlled CLI for the native Durable Workflow LangSmith experiment."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.provider_errors import sanitize_error_message

from .contracts import WORKFLOW_REGRESSION_DATASET
from .evaluators import REQUIRED_WORKFLOW_FEEDBACK_KEYS


_CASE_TYPES = (
    "parallel_join",
    "constraint_verifier",
    "minimal_repair",
    "interrupt_resume_equivalence",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or preflight the native Durable Workflow Experiment."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--allow-workflow-side-effects", action="store_true")
    args = parser.parse_args(argv)

    if args.inspect:
        _print(
            {
                "action": "inspect",
                "status": "offline_contract_ready",
                "dataset_name": WORKFLOW_REGRESSION_DATASET,
                "case_types": list(_CASE_TYPES),
                "feedback_keys": list(REQUIRED_WORKFLOW_FEEDBACK_KEYS),
                "operator_evidence": "pending",
            }
        )
        return 0
    action_name = "preflight" if args.preflight else "run"
    if not args.allow_real_provider:
        parser.error(f"--{action_name} requires --allow-real-provider")
    if not args.allow_workflow_side_effects:
        parser.error(f"--{action_name} requires --allow-workflow-side-effects")
    if args.run and not args.run_name:
        parser.error("--run requires --run-name")

    try:
        config = ProviderConfig.from_env()
        if config.provider_mode != "real":
            raise RuntimeError(
                "workflow Experiment requires MULTIMODAL_AGENT_PROVIDER_MODE=real"
            )
        config.validate_provider_mode()
        raise RuntimeError(
            "workflow Experiment production host is not available in this cutover"
        )
    except Exception as exc:
        _print(
            {
                "error": "langsmith_workflow_experiment_infrastructure_failure",
                "message": sanitize_error_message(exc),
            }
        )
        return 2


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _langsmith_client():
    from assistant_agent.observability.langsmith_config import (
        create_langsmith_client_from_env,
    )

    return create_langsmith_client_from_env()


__all__ = ["main"]
