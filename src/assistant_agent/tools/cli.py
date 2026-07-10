"""CLI helpers for validating local tool declarations."""

from __future__ import annotations

import argparse
import json
from typing import Any

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.tools import ToolPolicyMetadata
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.tools.loader import LocalToolLoadIssue, load_local_tools
from assistant_agent.tools.loader import register_local_tools
from assistant_agent.tools.registry import ToolRegistry, tool_policy_metadata


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _run_validate(args.module)
    if args.command == "simulate":
        return _run_simulate(args)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate assistant_agent local tools.")
    subparsers = parser.add_subparsers(dest="command")
    validate = subparsers.add_parser("validate", help="Validate explicit local tool modules.")
    validate.add_argument(
        "--module",
        action="append",
        default=[],
        help="Importable Python module exposing __assistant_tools__.",
    )
    simulate = subparsers.add_parser("simulate", help="Validate and execute one local tool through governance.")
    simulate.add_argument(
        "--module",
        action="append",
        default=[],
        help="Importable Python module exposing __assistant_tools__.",
    )
    simulate.add_argument("--tool", required=True, help="Tool name to simulate.")
    simulate.add_argument("--input", default="{}", help="Tool input as a JSON object.")
    simulate.add_argument("--text", default="", help="User request text for validation context.")
    simulate.add_argument("--user-id", default="local-tools-cli", help="Simulation user id.")
    simulate.add_argument("--session-id", default="local-tools-cli", help="Simulation session id.")
    simulate.add_argument("--realtime", action="store_true", help="Enable realtime risk-gate metadata.")
    return parser


def _run_validate(module_names: list[str]) -> int:
    load_result = load_local_tools(module_names)
    issues = list(load_result.issues)
    for local_tool in load_result.tools:
        issues.extend(_tool_validation_issues(local_tool))
    report = {
        "schema_version": "local_tools_validate_v1",
        "tool_count": len(load_result.tools),
        "tools": [getattr(local_tool, "name", "") for local_tool in load_result.tools],
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not issues else 1


def _run_simulate(args: argparse.Namespace) -> int:
    tool_input, input_issue = _parse_input_json(args.input)
    load_result = load_local_tools(args.module)
    issues = list(load_result.issues)
    for local_tool in load_result.tools:
        issues.extend(_tool_validation_issues(local_tool))
    if input_issue is not None:
        issues.append(input_issue)

    registry = ToolRegistry()
    if not issues:
        register_local_tools(registry, load_result.tools)

    report: dict[str, Any] = {
        "schema_version": "local_tools_simulate_v1",
        "tool_name": args.tool,
        "issues": [issue.model_dump(mode="json") for issue in issues],
    }
    if issues:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    metadata = {"realtime": {"run_id": "local-tools-simulate"}} if args.realtime else {}
    request = UserRequest(
        user_id=args.user_id,
        session_id=args.session_id,
        text=args.text,
        metadata=metadata,
    )
    state = AgentState.from_request(request, run_id="local-tools-simulate")
    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name=args.tool,
            tool_input=tool_input,
        ),
        registry=registry,
        request=request,
        state=state,
    )
    report["validation"] = validation.model_dump(mode="json")
    if not validation.accepted:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    sink = ListEventSink()
    result = ToolExecutor(registry=registry, event_sink=sink).run_tool(
        state,
        "simulate",
        args.tool,
        tool_input,
    )
    observation = observation_from_tool_result(result, request_text=request.text)
    finished = next(
        (event for event in sink.events if event.type in {"tool_finished", "tool_failed"}),
        None,
    )
    report.update(
        {
            "result": result.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
            "post_tool_call": (
                finished.payload.get("post_tool_call")
                if finished is not None and isinstance(finished.payload, dict)
                else None
            ),
        }
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.success else 1


def _parse_input_json(value: str) -> tuple[dict[str, Any], LocalToolLoadIssue | None]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return {}, LocalToolLoadIssue(code="invalid_input_json", message=str(exc))
    if not isinstance(parsed, dict):
        return {}, LocalToolLoadIssue(
            code="invalid_input_json",
            message="--input must be a JSON object.",
        )
    return parsed, None


def _tool_validation_issues(tool: Any) -> list[LocalToolLoadIssue]:
    tool_name = getattr(tool, "name", "")
    issues: list[LocalToolLoadIssue] = []
    try:
        policy = tool_policy_metadata(tool)
    except Exception as exc:
        return [
            LocalToolLoadIssue(
                code="invalid_policy",
                message=str(exc),
                tool_name=tool_name,
            )
        ]
    if policy is None:
        return [
            LocalToolLoadIssue(
                code="missing_policy",
                message="Local tools must declare ToolPolicyMetadata.",
                tool_name=tool_name,
            )
        ]
    issues.extend(_policy_validation_issues(tool_name=tool_name, policy=policy))
    return issues


def _policy_validation_issues(
    *,
    tool_name: str,
    policy: ToolPolicyMetadata,
) -> list[LocalToolLoadIssue]:
    issues: list[LocalToolLoadIssue] = []
    if policy.execution.timeout_s is None:
        issues.append(
            LocalToolLoadIssue(
                code="missing_timeout",
                message="Local tools must declare execution.timeout_s.",
                tool_name=tool_name,
            )
        )
    if (
        policy.data.reads_private_data
        or policy.data.writes_private_data
        or policy.data.sends_data_external
    ) and not policy.data.redact_in_trace:
        issues.append(
            LocalToolLoadIssue(
                code="missing_trace_redaction",
                message="Private or external-data tools must set data.redact_in_trace.",
                tool_name=tool_name,
            )
        )
    return issues


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
