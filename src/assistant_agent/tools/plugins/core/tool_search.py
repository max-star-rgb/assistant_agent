"""Governed discovery tool for deferred Registry and configured MCP tools."""

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
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.schemas.tool_ids import TOOL_SEARCH_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import ToolInputBinding


class ToolSearchTool(ToolBase):
    """Discover already-governed deferred or MCP tools without executing them."""

    name = TOOL_SEARCH_TOOL_NAME
    description = (
        "仅当当前直接暴露的工具无法满足请求时，搜索本轮已授权的延迟工具目录或已配置 MCP 工具。"
        "此工具只发现候选；延迟工具会在下一轮加载完整 Schema，所有执行仍经过统一治理。"
    )
    input_schema = ToolSearchInput
    output_schema = ToolSearchResult
    category = "read"
    requires_confirmation = False
    defer_loading = False
    input_bindings = (
        ToolInputBinding(field="limit", source="constant", value=8),
        ToolInputBinding(
            field="include_permission_required",
            source="constant",
            value=True,
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
        self._registry_specs: dict[str, ToolSpec] = {}
        self._registry_catalog_bound = False

    def bind_registry_catalog(self, specs: list[ToolSpec]) -> None:
        """Bind the immutable startup inventory before Registry sealing."""

        if self._registry_catalog_bound:
            raise RuntimeError("Tool discovery catalog is already bound.")
        self._registry_specs = {
            spec.name: spec.model_copy(deep=True)
            for spec in specs
            if spec.name != self.name
        }
        self._registry_catalog_bound = True

    def _run(self, input: ToolSearchInput, context: ToolContext) -> ToolResult:
        result = self.search(
            input,
            allowed_registry_names=_deferred_registry_names(context),
        )
        data = result.model_dump(mode="json")
        activated_names = [
            candidate.tool_name
            for candidate in result.matches
            if candidate.source == "registry"
            and candidate.status == "enabled"
        ]
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=data,
            tool_catalog_activation=activated_names,
        )

    def search(
        self,
        input: ToolSearchInput,
        *,
        allowed_registry_names: set[str] | None = None,
    ) -> ToolSearchResult:
        """Return prompt-safe candidates from already-governed discovery spaces."""

        servers = _selected_servers(self.server_configs, input.server_name)
        candidates: list[ToolSearchCandidate] = []
        issues: list[str] = []
        omitted_unallowlisted_count = 0
        searched_server_names: list[str] = []
        deferred_names = set(allowed_registry_names or ())
        if input.server_name is None:
            for tool_name in sorted(deferred_names):
                spec = self._registry_specs.get(tool_name)
                if spec is None:
                    continue
                score = _registry_match_score(input.query, spec)
                if input.query.strip() and score <= 0:
                    continue
                candidates.append(
                    ToolSearchCandidate(
                        tool_name=spec.name,
                        source="registry",
                        description=_clip(spec.description, 240),
                        status="enabled",
                        permission_required=False,
                        read_only=spec.category == "read",
                        side_effect_level=(
                            "external_read"
                            if spec.category == "read"
                            else "pending_confirmation"
                        ),
                        required_inputs=list(spec.input_schema.get("required", [])),
                        input_fields=_input_fields(spec.input_schema),
                        match_score=score,
                    )
                )

        runner = self._runner() if servers else None
        for server in servers:
            searched_server_names.append(server.server_name)
            try:
                assert runner is not None
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
                candidates.append(
                    ToolSearchCandidate(
                        tool_name=spec.name,
                        source="mcp",
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
                        side_effect_level=(
                            "external_read"
                            if spec.category == "read"
                            else "pending_confirmation"
                        ),
                        required_inputs=list(spec.input_schema.get("required", [])),
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
            deferred_registry_count=len(deferred_names),
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


def _registry_match_score(query: str, spec: ToolSpec) -> int:
    terms = _tokens(query)
    if not terms:
        return 1
    properties = spec.input_schema.get("properties")
    field_names = (
        [name for name in properties if isinstance(name, str)]
        if isinstance(properties, dict)
        else []
    )
    return _match_score_values(
        terms,
        " ".join((spec.name, spec.description, *field_names)),
    )


def _match_score_values(terms: list[str], haystack: str) -> int:
    haystack_tokens = set(_tokens(haystack))
    normalized_haystack = _normalize_text(haystack)
    score = 0
    for term in terms:
        if term in haystack_tokens:
            score += 3
        elif term in normalized_haystack:
            score += 1
    return score


def _deferred_registry_names(context: ToolContext) -> set[str]:
    catalog = context.metadata.get("run_tool_catalog")
    if not isinstance(catalog, dict):
        return set()
    excluded = catalog.get("excluded_reasons")
    if not isinstance(excluded, dict):
        return set()
    return {
        name
        for name, reasons in excluded.items()
        if isinstance(name, str)
        and isinstance(reasons, list)
        and "deferred_for_schema_budget" in reasons
    }


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
        return "No matching deferred or configured MCP tools were found."
    permission_required = sum(1 for item in matches if item.permission_required)
    enabled = len(matches) - permission_required
    issue_part = f" Issues: {len(issues)}." if issues else ""
    return (
        f"Found {len(matches)} matching governed tools"
        f" ({enabled} enabled, {permission_required} permission required)"
        f" out of {total_matches} total matches.{issue_part}"
    )


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."
