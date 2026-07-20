"""Governed discovery tool for configured MCP server tools."""

from __future__ import annotations

import re
from typing import Any

from assistant_agent.mcp.adapter import MCPToolAdapter, MCPToolDefinition
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.schemas.tool_search import (
    ToolSearchCandidate,
    ToolSearchInput,
    ToolSearchInputField,
    ToolSearchResult,
)
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    DataPolicy,
    ExecutionPolicy,
    RealtimeToolPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
    VisibilityPolicy,
)
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.services.tool_manifest import TOOL_SEARCH_TOOL_NAME
from assistant_agent.tools.base import MockTool, ToolContext


class ToolSearchTool(MockTool):
    """Inspect configured MCP servers for fallback tools without executing them."""

    name = TOOL_SEARCH_TOOL_NAME
    description = (
        "Search configured MCP servers for additional tools only when the exposed core tools "
        "cannot satisfy the user request. This discovers candidates; it does not execute them "
        "or grant permission to execute them."
    )
    input_schema = ToolSearchInput
    output_schema = ToolSearchResult
    execution = ToolExecutionPolicy(
        dependency_mode="requires_prior_observation",
        resource_reads=["mcp.tool_catalog"],
        realtime_safety="safe",
        artifact_reuse="reusable",
        progress_message="我看一下还有哪些可用工具。",
    )
    policy = ToolPolicyMetadata(
        risk="local_read",
        realtime=RealtimeToolPolicy(mode="inline"),
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(timeout_s=5, retry_count=0, max_result_chars=4000),
        data=DataPolicy(redact_in_trace=True),
        visibility=VisibilityPolicy(
            toolset="tool.discovery",
            tags=["tool_search", "mcp"],
        ),
    )

    def __init__(
        self,
        *,
        server_configs: list[MCPServerConfig] | None = None,
        runner: Any | None = None,
    ) -> None:
        self.server_configs = list(server_configs or [])
        self.runner = runner

    def _run(self, input: ToolSearchInput, context: ToolContext) -> ToolResult:
        result = self.search(input)
        data = result.model_dump(mode="json")
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=data,
        )

    def search(self, input: ToolSearchInput) -> ToolSearchResult:
        """Return prompt-safe MCP discovery candidates for the requested capability."""

        servers = _selected_servers(self.server_configs, input.server_name)
        if not servers:
            return ToolSearchResult(
                query=input.query,
                configured_server_count=len(self.server_configs),
                summary="No configured MCP servers matched the discovery request.",
            )
        runner = self._runner()
        candidates: list[ToolSearchCandidate] = []
        issues: list[str] = []
        omitted_unallowlisted_count = 0
        searched_server_names: list[str] = []
        for server in servers:
            searched_server_names.append(server.server_name)
            try:
                definitions = runner.list_tools(server=server)
            except Exception as exc:  # pragma: no cover - defensive discovery boundary
                issues.append(f"{server.server_name}: {sanitize_error_message(exc)}")
                continue
            adapter = MCPToolAdapter(server.adapter_config())
            for raw_definition in definitions:
                definition = _definition_from_raw(raw_definition)
                if definition is None:
                    issues.append(f"{server.server_name}: invalid MCP tool definition")
                    continue
                if not server.adapter_config().is_allowed(definition.name):
                    omitted_unallowlisted_count += 1
                    continue
                spec = adapter.tool_spec_for_definition(definition)
                if spec is None:
                    omitted_unallowlisted_count += 1
                    continue
                status = (
                    "enabled"
                    if server.adapter_config().is_enabled_by_default(definition.name)
                    else "permission_required"
                )
                if status == "permission_required" and not input.include_permission_required:
                    continue
                score = _match_score(
                    input.query,
                    server=server,
                    definition=definition,
                    spec_name=spec.name,
                )
                if input.query.strip() and score <= 0:
                    continue
                from assistant_agent.services.tool_policy import ToolPolicyInterpreter

                policy = ToolPolicyInterpreter().view_for_spec(spec)
                candidates.append(
                    ToolSearchCandidate(
                        tool_name=spec.name,
                        server_name=server.server_name,
                        mcp_tool_name=definition.name,
                        description=_clip(definition.description, 240),
                        status=status,
                        permission_required=status == "permission_required",
                        permission_hint=(
                            _permission_hint(spec.name)
                            if status == "permission_required"
                            else None
                        ),
                        read_only=server.adapter_config().is_read_only(definition.name),
                        side_effect_level=policy.side_effect_level,
                        required_inputs=list(spec.required_inputs),
                        input_fields=_input_fields(definition.input_schema),
                        match_score=score,
                    )
                )
        candidates.sort(
            key=lambda item: (
                -item.match_score,
                item.permission_required,
                item.tool_name,
            )
        )
        limited = candidates[: input.limit]
        return ToolSearchResult(
            query=input.query,
            matches=limited,
            total_matches=len(candidates),
            configured_server_count=len(self.server_configs),
            searched_server_names=searched_server_names,
            omitted_unallowlisted_count=omitted_unallowlisted_count,
            issues=issues,
            summary=_summary(limited, total_matches=len(candidates), issues=issues),
        )

    def _runner(self) -> Any:
        if self.runner is not None:
            return self.runner
        try:
            from assistant_agent.mcp.sdk_client import SdkMCPClientRunner

            return SdkMCPClientRunner(self.server_configs)
        except ImportError:
            from assistant_agent.mcp.stdio_client import StdioMCPClientRunner

            return StdioMCPClientRunner(self.server_configs)


def _selected_servers(
    server_configs: list[MCPServerConfig],
    server_name: str | None,
) -> list[MCPServerConfig]:
    if not server_name:
        return list(server_configs)
    return [server for server in server_configs if server.server_name == server_name]


def _definition_from_raw(raw: Any) -> MCPToolDefinition | None:
    if isinstance(raw, MCPToolDefinition):
        return raw
    try:
        if isinstance(raw, dict):
            return MCPToolDefinition.model_validate(raw)
        return MCPToolDefinition(
            name=str(getattr(raw, "name")),
            description=str(getattr(raw, "description", "") or ""),
            input_schema=dict(getattr(raw, "input_schema", {}) or {}),
        )
    except Exception:
        return None


def _input_fields(input_schema: dict[str, Any]) -> list[ToolSearchInputField]:
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = {
        str(item)
        for item in input_schema.get("required", [])
        if isinstance(item, str)
    }
    fields: list[ToolSearchInputField] = []
    for name, schema in properties.items():
        if not isinstance(name, str) or not isinstance(schema, dict):
            continue
        fields.append(
            ToolSearchInputField(
                name=name,
                type=str(schema.get("type") or "string"),
                required=name in required,
                description=_clip(str(schema.get("description") or ""), 120),
            )
        )
    return fields


def _match_score(
    query: str,
    *,
    server: MCPServerConfig,
    definition: MCPToolDefinition,
    spec_name: str,
) -> int:
    terms = _tokens(query)
    if not terms:
        return 1
    haystack = " ".join(
        (
            spec_name,
            server.server_name,
            definition.name,
            definition.description,
            " ".join(_input_field_names(definition.input_schema)),
        )
    )
    haystack_tokens = set(_tokens(haystack))
    score = 0
    normalized_haystack = _normalize_text(haystack)
    for term in terms:
        if term in haystack_tokens:
            score += 3
        elif term in normalized_haystack:
            score += 1
    return score


def _tokens(value: str) -> list[str]:
    normalized = _normalize_text(value)
    return [item for item in normalized.split() if item]


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value.lower()).strip()


def _input_field_names(input_schema: dict[str, Any]) -> list[str]:
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    return [name for name in properties if isinstance(name, str)]


def _permission_hint(tool_name: str) -> str:
    return (
        f"{tool_name} is configured but not enabled by default. Ask the user or operator "
        f"to enable it by adding {tool_name} to request.metadata.tool_visibility.enabled_tools; "
        "after permission is granted, execution still must pass ActionValidator and ToolExecutor."
    )


def _summary(
    matches: list[ToolSearchCandidate],
    *,
    total_matches: int,
    issues: list[str],
) -> str:
    if not matches and not issues:
        return "No matching configured MCP tools were found."
    permission_required = sum(1 for item in matches if item.permission_required)
    enabled = len(matches) - permission_required
    issue_part = f" Issues: {len(issues)}." if issues else ""
    return (
        f"Found {len(matches)} matching MCP tools"
        f" ({enabled} enabled, {permission_required} permission required)"
        f" out of {total_matches} total matches.{issue_part}"
    )


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."
