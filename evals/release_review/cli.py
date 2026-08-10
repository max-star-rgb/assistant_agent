from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Sequence

from langfuse import Langfuse

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.providers.provider_http import without_unsupported_socks_proxy_env
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.local_calendar import (
    LocalSQLiteCalendarAdapter,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry

from .catalog import build_catalog_snapshot
from .experiment import ReleaseExperimentSettings
from .loader import load_scenarios
from .service import ReleaseReviewRequest, ReleaseReviewService
from .staging import LocalStagingProfileAdapter, StagingResourceManager
from .sync_dataset import sync_release_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO_ROOT = PROJECT_ROOT / "evals" / "release_review" / "scenarios"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / ".data" / "evals" / "release_review"
EVALUATOR_VERSION = "release-review-rule-v1"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the native Langfuse pre-release Agent review."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--sync", action="store_true")
    action.add_argument("--run", action="store_true")
    action.add_argument("--record-decision", action="store_true")
    parser.add_argument("--scenario-root", type=Path, default=DEFAULT_SCENARIO_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--release-id")
    parser.add_argument("--run-name")
    parser.add_argument("--scenario", action="append", dest="scenario_ids")
    parser.add_argument("--git-commit")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--allow-staging-side-effects", action="store_true")
    parser.add_argument("--experiment-run-id")
    parser.add_argument(
        "--decision", choices=("approved", "approved_with_risk", "rejected")
    )
    parser.add_argument("--operator")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)

    try:
        if args.inspect:
            scenarios = load_scenarios(args.scenario_root)
            print(
                json.dumps(
                    {
                        "action": "inspect",
                        "dataset_name": "assistant-agent-release-review",
                        "scenario_count": len(scenarios),
                        "decision_count": sum(item.phase == "decision" for item in scenarios),
                        "staging_count": sum(item.phase == "staging" for item in scenarios),
                        "dataset_item_count": sum(item.repetitions for item in scenarios),
                        "scenarios": [
                            {
                                "id": item.id,
                                "phase": item.phase,
                                "risk": item.risk,
                                "repetitions": item.repetitions,
                            }
                            for item in scenarios
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.record_decision:
            _require_args(
                parser,
                args,
                "release_id",
                "experiment_run_id",
                "decision",
                "operator",
            )
            record = ReleaseReviewService.for_decisions_only(
                args.artifact_root
            ).record_release_decision(
                release_id=args.release_id,
                experiment_run_id=args.experiment_run_id,
                decision=args.decision,
                operator=args.operator,
                note=args.note,
            )
            print(record.model_dump_json())
            return 0

        if not args.no_env_file:
            load_env_file(args.env_file)
        scenarios = load_scenarios(args.scenario_root)
        client = _langfuse_client()
        git_commit = args.git_commit or _git_commit()
        if args.sync:
            result = sync_release_dataset(client, scenarios, git_commit)
            client.flush()
            print(
                json.dumps(
                    result.__dict__,
                    ensure_ascii=False,
                    default=list,
                    indent=2,
                )
            )
            return 0

        _require_args(parser, args, "release_id")
        if not args.allow_real_provider:
            parser.error("--run requires --allow-real-provider")
        selected_ids = tuple(args.scenario_ids) if args.scenario_ids else None
        if _selection_requires_staging(scenarios, selected_ids) and not args.allow_staging_side_effects:
            parser.error("--run with Staging scenarios requires --allow-staging-side-effects")
        config = ProviderConfig.from_env()
        _validate_real_config(config)
        request = ReleaseReviewRequest(
            release_id=args.release_id,
            scenario_ids=selected_ids,
            run_name=args.run_name,
        )
        service = _build_service(
            client=client,
            config=config,
            request=request,
            scenario_root=args.scenario_root,
            artifact_root=args.artifact_root,
            git_commit=git_commit,
        )
        report = service.run(request)
        print(report.model_dump_json(indent=2))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": "release_review_infrastructure_failure",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2


def _selection_requires_staging(
    scenarios: Sequence[object], scenario_ids: tuple[str, ...] | None
) -> bool:
    selected = None if scenario_ids is None else set(scenario_ids)
    return any(
        getattr(item, "phase", None) == "staging"
        and (selected is None or getattr(item, "id", None) in selected)
        for item in scenarios
    )


def _build_service(
    *,
    client: Langfuse,
    config: ProviderConfig,
    request: ReleaseReviewRequest,
    scenario_root: Path,
    artifact_root: Path,
    git_commit: str,
) -> ReleaseReviewService:
    staging_root = artifact_root / request.release_id / "staging"
    manager = StagingResourceManager(
        {
            profile: LocalStagingProfileAdapter(profile, staging_root)
            for profile in (
                "deep_research_workflow",
                "amap_readonly",
                "test_calendar",
            )
        }
    )
    probe = AgentGraphRuntime(config=config)
    try:
        catalog = build_catalog_snapshot(probe.registry)
    finally:
        probe.close()

    def runtime_factory(scenario, backend, runtime_metadata):
        paths = runtime_metadata.get("staging_paths", {})
        scenario_config = config
        registry = None
        if scenario.phase == "staging" and scenario.staging is not None:
            if scenario.staging.resource_profile == "deep_research_workflow":
                scenario_config = replace(
                    config,
                    durable_workflows_enabled=True,
                    durable_workflow_path=str(paths["workflow_db"]),
                    durable_workflow_artifact_path=str(paths["workflow_artifacts"]),
                )
            elif scenario.staging.resource_profile == "test_calendar":
                calendar = LocalSQLiteCalendarAdapter(paths["calendar_db"])
                registry = create_default_registry(
                    scenario_config,
                    calendar_adapter=calendar,
                )
        return AgentGraphRuntime(
            registry=registry,
            config=scenario_config,
            tool_execution_backend=backend,
        )

    def settings_factory(review_request, selected):
        catalog.require_tools(
            tool_name
            for scenario in selected
            for tool_name in scenario.tool_contract.required
        )
        return ReleaseExperimentSettings(
            release_id=review_request.release_id,
            model=config.resolved_chat_provider().model,
            git_commit=git_commit,
            catalog_generation=catalog.generation,
            evaluator_version=EVALUATOR_VERSION,
            runtime_factory=runtime_factory,
            staging_resources=manager,
            run_name=review_request.run_name,
            deadline_monotonic=monotonic() + 570,
        )

    return ReleaseReviewService(
        client=client,
        scenario_root=scenario_root,
        artifact_root=artifact_root,
        settings_factory=settings_factory,
        progress=_emit_progress,
    )


def _validate_real_config(config: ProviderConfig) -> None:
    if config.provider_mode != "real":
        raise RuntimeError("Release Review requires MULTIMODAL_AGENT_PROVIDER_MODE=real")
    config.validate_provider_mode()


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


def _git_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _require_args(parser: argparse.ArgumentParser, args: argparse.Namespace, *names: str) -> None:
    missing = [f"--{name.replace('_', '-')}" for name in names if not getattr(args, name)]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))


def _emit_progress(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
