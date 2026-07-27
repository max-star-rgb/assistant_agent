"""Explicit CLI helpers for workflow skill manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.tool_workflow_skill import (
    InMemoryWorkflowSkillRunStore,
    JsonlWorkflowSkillRunStore,
    WorkflowSkillCatalog,
    WorkflowSkillLauncher,
    WorkflowSkillRunQueryService,
    WorkflowSkillRunResult,
)
from assistant_agent.tools.loader import LocalToolLoadIssue, load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _run_validate(args)
    if args.command == "launch":
        return _run_launch(args)
    if args.command == "resume":
        return _run_resume(args)
    if args.command == "summary":
        return _run_summary(args)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and launch explicit workflow_skill_v1 manifests."
    )
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser("validate", help="Validate one workflow manifest.")
    _add_manifest_args(validate)

    launch = subparsers.add_parser("launch", help="Launch one explicitly registered workflow.")
    _add_manifest_args(launch)
    launch.add_argument("--workflow", required=True, help="Registered workflow manifest name.")
    launch.add_argument("--text", default="", help="User request text.")
    launch.add_argument("--user-id", default="workflow-skill-cli", help="User id.")
    launch.add_argument("--session-id", default="workflow-skill-cli", help="Session id.")
    launch.add_argument("--run-id", default="workflow-skill-cli", help="Run id.")
    launch.add_argument("--run-store", help="Optional JSONL run store path.")

    resume = subparsers.add_parser("resume", help="Resume one workflow run from a JSONL store.")
    _add_manifest_args(resume)
    resume.add_argument("--run-id", required=True, help="Workflow run id to resume.")
    resume.add_argument("--run-store", required=True, help="JSONL run store path.")
    resume.add_argument("--text", default="", help="User request text for resume context.")
    resume.add_argument("--user-id", default="workflow-skill-cli", help="User id.")
    resume.add_argument("--session-id", default="workflow-skill-cli", help="Session id.")

    summary = subparsers.add_parser("summary", help="Print a prompt-safe workflow run summary.")
    summary.add_argument("--run-store", required=True, help="JSONL run store path.")
    summary.add_argument("--run-id", required=True, help="Workflow run id.")
    return parser


def _add_manifest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, help="Path to a workflow_skill_v1 JSON manifest.")
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="Importable Python module exposing governed local tools.",
    )


def _run_validate(args: argparse.Namespace) -> int:
    registry, issues = _registry_from_modules(args.module)
    manifest, manifest_issue = _load_manifest(args.manifest)
    if manifest_issue is not None:
        issues.append(manifest_issue)
    catalog = WorkflowSkillCatalog(registry=registry)
    validation = (
        catalog.register(manifest)
        if manifest is not None
        else None
    )
    report = {
        "schema_version": "workflow_skill_cli_validate_v1",
        "accepted": validation.accepted if validation is not None and not issues else False,
        "workflow_id": validation.manifest.name if validation is not None and validation.manifest else None,
        "registered_tools": registry.list(),
        "issues": _issue_payloads(issues)
        + (
            [issue.model_dump(mode="json") for issue in validation.issues]
            if validation is not None
            else []
        ),
    }
    _print_json(report)
    return 0 if report["accepted"] else 1


def _run_launch(args: argparse.Namespace) -> int:
    registry, issues = _registry_from_modules(args.module)
    manifest, manifest_issue = _load_manifest(args.manifest)
    if manifest_issue is not None:
        issues.append(manifest_issue)
    catalog = WorkflowSkillCatalog(registry=registry)
    registration = catalog.register(manifest) if manifest is not None else None
    if issues or registration is None or not registration.accepted:
        report = {
            "schema_version": "workflow_skill_cli_launch_v1",
            "accepted": False,
            "issues": _issue_payloads(issues)
            + (
                [issue.model_dump(mode="json") for issue in registration.issues]
                if registration is not None
                else []
            ),
        }
        _print_json(report)
        return 1

    store = _run_store(args.run_store)
    launcher = WorkflowSkillLauncher(catalog=catalog, run_store=store)
    state = AgentState.from_request(
        UserRequest(
            user_id=args.user_id,
            session_id=args.session_id,
            text=args.text,
        ),
        run_id=args.run_id,
    )
    result = launcher.launch(args.workflow, state)
    summary = WorkflowSkillRunQueryService(store=store).get_run_summary(state.run_id)
    report = {
        "schema_version": "workflow_skill_cli_launch_v1",
        "accepted": result.status != "validation_failed",
        "result": _result_payload(result),
        "summary": summary.model_dump(mode="json") if summary is not None else None,
    }
    _print_json(report)
    return _exit_code_for_result(result)


def _run_resume(args: argparse.Namespace) -> int:
    registry, issues = _registry_from_modules(args.module)
    manifest, manifest_issue = _load_manifest(args.manifest)
    if manifest_issue is not None:
        issues.append(manifest_issue)
    catalog = WorkflowSkillCatalog(registry=registry)
    registration = catalog.register(manifest) if manifest is not None else None
    if issues or registration is None or not registration.accepted:
        report = {
            "schema_version": "workflow_skill_cli_resume_v1",
            "accepted": False,
            "issues": _issue_payloads(issues)
            + (
                [issue.model_dump(mode="json") for issue in registration.issues]
                if registration is not None
                else []
            ),
        }
        _print_json(report)
        return 1

    store = JsonlWorkflowSkillRunStore(args.run_store)
    launcher = WorkflowSkillLauncher(catalog=catalog, run_store=store)
    result = launcher.resume(
        args.run_id,
        AgentState.from_request(
            UserRequest(
                user_id=args.user_id,
                session_id=args.session_id,
                text=args.text,
            ),
        ),
    )
    summary = WorkflowSkillRunQueryService(store=store).get_run_summary(args.run_id)
    report = {
        "schema_version": "workflow_skill_cli_resume_v1",
        "accepted": result.status != "validation_failed",
        "result": _result_payload(result),
        "summary": summary.model_dump(mode="json") if summary is not None else None,
    }
    _print_json(report)
    return _exit_code_for_result(result)


def _run_summary(args: argparse.Namespace) -> int:
    store = JsonlWorkflowSkillRunStore(args.run_store)
    summary = WorkflowSkillRunQueryService(store=store).get_run_summary(args.run_id)
    report = {
        "schema_version": "workflow_skill_cli_summary_v1",
        "summary": summary.model_dump(mode="json") if summary is not None else None,
    }
    _print_json(report)
    return 0 if summary is not None else 1


def _registry_from_modules(module_names: list[str]) -> tuple[ToolRegistry, list[LocalToolLoadIssue]]:
    load_result = load_local_tools(module_names)
    issues = list(load_result.issues)
    for local_tool in load_result.tools:
        issues.extend(_local_tool_policy_issues(local_tool))
    registry = ToolRegistry()
    if not issues:
        register_local_tools(registry, load_result.tools)
    return registry, issues


def _local_tool_policy_issues(tool: Any) -> list[LocalToolLoadIssue]:
    try:
        ToolRegistry._tool_spec(tool)
    except Exception as exc:
        return [
            LocalToolLoadIssue(
                code="invalid_tool_spec",
                message=str(exc),
                tool_name=getattr(tool, "name", ""),
            )
        ]
    return []


def _load_manifest(path: str) -> tuple[dict[str, Any] | None, LocalToolLoadIssue | None]:
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return None, LocalToolLoadIssue(code="manifest_read_failed", message=str(exc))
    except json.JSONDecodeError as exc:
        return None, LocalToolLoadIssue(code="invalid_manifest_json", message=str(exc))
    if not isinstance(parsed, dict):
        return None, LocalToolLoadIssue(
            code="invalid_manifest_json",
            message="Workflow manifest must be a JSON object.",
        )
    return parsed, None


def _run_store(path: str | None) -> InMemoryWorkflowSkillRunStore | JsonlWorkflowSkillRunStore:
    if path:
        return JsonlWorkflowSkillRunStore(path)
    return InMemoryWorkflowSkillRunStore()


def _result_payload(result: WorkflowSkillRunResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "status": result.status,
        "workflow_id": result.workflow_id,
        "attempt_count": len(result.attempts),
        "issues": [issue.model_dump(mode="json") for issue in result.issues],
    }


def _exit_code_for_result(result: WorkflowSkillRunResult) -> int:
    return 0 if result.status == "succeeded" else 1


def _issue_payloads(issues: list[LocalToolLoadIssue]) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in issues]


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
