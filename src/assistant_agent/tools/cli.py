"""CLI helpers for validating local tool declarations."""

from __future__ import annotations

import argparse
import json
from typing import Any

from assistant_agent.schemas.tools import ToolPolicyMetadata
from assistant_agent.tools.loader import LocalToolLoadIssue, load_local_tools
from assistant_agent.tools.registry import tool_policy_metadata


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _run_validate(args.module)
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
