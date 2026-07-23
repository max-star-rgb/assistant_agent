"""Tool registry and default tool registration."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Dict, Iterable, List

from pydantic import BaseModel

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.services.video_context import VideoContextStore
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.tools.input_binding import (
    runtime_owned_input_fields,
    validate_tool_input_bindings,
)
from assistant_agent.tools.plugins.assembly import (
    ToolContribution,
    ToolPluginAssemblyError,
    assemble_tool_plugins,
    configured_plugin_modules_from_env,
    normalize_configured_plugin_modules,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginAssemblyReport,
    ToolPluginContext,
    ToolPluginLoadIssue,
    ToolPluginSourceRecord,
    ToolRegistrationRecord,
)
from assistant_agent.tools.plugins.defaults import default_tool_plugins
from assistant_agent.tools.plugins.vision.plugin import (
    build_realtime_video_observation_tool,
)

if TYPE_CHECKING:
    from assistant_agent.mcp.config import MCPServerConfig
    from assistant_agent.mcp.registration import MCPToolDiscoveryRunner
    from assistant_agent.services.durable_tasks.service import DurableTaskService


_REGISTERED_TOOL_CONTRACTS: dict[str, dict[str, Any]] = {}


class ToolRegistry:
    """In-memory registry for tool lookup and execution."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._registration_records: dict[str, ToolRegistrationRecord] = {}
        self._sealed = False
        self._generation: str | None = None
        self._assembly_report = ToolPluginAssemblyReport()

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def generation(self) -> str | None:
        return self._generation

    @property
    def assembly_report(self) -> ToolPluginAssemblyReport:
        return self._assembly_report.model_copy(deep=True)

    def register(
        self,
        tool: Tool,
        registration: ToolRegistrationRecord | None = None,
    ) -> None:
        if self._sealed:
            raise RuntimeError("ToolRegistry is sealed and cannot be modified.")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        spec = self._tool_spec(tool)
        record = registration or ToolRegistrationRecord(
            tool_name=tool.name,
            plugin_id="manual",
            plugin_version="unversioned",
            source_type="manual",
            source_ref=tool.__class__.__module__,
        )
        if record.tool_name != tool.name:
            raise ValueError("Tool registration record name does not match Tool name.")
        self._tools[tool.name] = tool
        self._registration_records[tool.name] = record
        _REGISTERED_TOOL_CONTRACTS[tool.name] = {
            "category": spec.category,
            "requires_confirmation": spec.requires_confirmation,
        }

    def register_many(self, contributions: Iterable[ToolContribution]) -> None:
        """Validate a complete batch before committing any Tool."""

        if self._sealed:
            raise RuntimeError("ToolRegistry is sealed and cannot be modified.")
        pending = list(contributions)
        names: set[str] = set(self._tools)
        for contribution in pending:
            tool = contribution.tool
            if tool.name in names:
                raise ValueError(f"Tool already registered: {tool.name}")
            if contribution.registration.tool_name != tool.name:
                raise ValueError("Tool registration record name does not match Tool name.")
            self._tool_spec(tool)
            names.add(tool.name)
        for contribution in pending:
            self.register(contribution.tool, contribution.registration)

    def seal(self, *, assembly_report: ToolPluginAssemblyReport | None = None) -> None:
        """Finalize a deterministic immutable startup generation."""

        if self._sealed:
            return
        payload = [
            {
                "registration": self._registration_records[name].model_dump(mode="json"),
                "spec": self.get_spec(name).model_dump(mode="json"),
            }
            for name in sorted(self._tools)
        ]
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._generation = f"sha256:{digest}"
        self._assembly_report = (
            assembly_report.model_copy(deep=True)
            if assembly_report is not None
            else ToolPluginAssemblyReport(
                registrations=self.list_registration_records()
            )
        )
        self._sealed = True

    def registration_record(self, tool_name: str) -> ToolRegistrationRecord:
        try:
            return self._registration_records[tool_name].model_copy(deep=True)
        except KeyError as exc:
            raise KeyError(f"Tool not registered: {tool_name}") from exc

    def list_registration_records(self) -> list[ToolRegistrationRecord]:
        return [
            self._registration_records[name].model_copy(deep=True)
            for name in sorted(self._registration_records)
        ]

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool not registered: {name}") from exc

    def list(self) -> list[str]:
        return sorted(self._tools)

    def run(
        self,
        name: str,
        input: BaseModel | dict[str, Any],
        context: ToolContext | None = None,
    ) -> ToolResult:
        return self.get(name).run(input, context)

    def get_spec(self, name: str) -> ToolSpec:
        """Return the provider-neutral contract for one registered tool."""

        return self._tool_spec(self.get(name))

    def list_specs(self) -> list[ToolSpec]:
        """Return provider-neutral specs for all registered tools."""

        return [self._tool_spec(self._tools[name]) for name in sorted(self._tools)]

    def host_configured_tool_names(self) -> set[str]:
        """Return built-in Tool exposure exceptions approved by the host."""

        return {
            name
            for name, tool in self._tools.items()
            if self._registration_records[name].source_type == "builtin"
            and getattr(tool, "host_configured_exposure", False) is True
        }

    @staticmethod
    def _tool_spec(tool: Tool) -> ToolSpec:
        validate_tool_input_bindings(tool)
        return ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=_schema_to_dict(
                tool.input_schema,
                hidden_fields=runtime_owned_input_fields(tool),
            ),
            **_declared_contract(tool),
        )

    def describe_tools(self) -> List[Dict[str, Any]]:
        """Return legacy dict descriptions of all registered tools for the assistant."""

        return [spec.model_dump(mode="json") for spec in self.list_specs()]


def _schema_to_dict(schema_type, *, hidden_fields: Iterable[str] = ()):
    """Convert a Pydantic model to the model-visible semantic input schema."""
    try:
        schema = schema_type.model_json_schema()
        definitions = schema.get("$defs", {})
        normalized = _inline_local_schema_refs(schema, definitions)
        normalized.pop("$defs", None)
        properties = normalized.get("properties", {})
        required = list(normalized.get("required", []))
        hidden = set(hidden_fields)
        for field_name in list(properties):
            if field_name in hidden:
                properties.pop(field_name, None)
                required = [item for item in required if item != field_name]
        normalized["properties"] = properties
        normalized["required"] = required
        return normalized
    except Exception:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }


def _inline_local_schema_refs(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_inline_local_schema_refs(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        target = definitions.get(name, {})
        merged = {**target, **{key: item for key, item in value.items() if key != "$ref"}}
        return _inline_local_schema_refs(merged, definitions)
    return {
        key: _inline_local_schema_refs(item, definitions)
        for key, item in value.items()
        if key != "$defs"
    }


def _declared_contract(tool: Tool) -> dict[str, Any]:
    """Read the small set of ToolSpec fields a local or MCP tool may declare."""

    fields = (
        "category",
        "toolset",
        "requires_confirmation",
        "enabled_by_default",
        "requires_media",
    )
    return {name: getattr(tool, name) for name in fields if hasattr(tool, name)}


def tool_contract_fields(tool_name: str) -> dict[str, Any]:
    """Return the last registered contract for legacy realtime projections."""

    return {
        "category": "dangerous",
        "requires_confirmation": True,
        **_REGISTERED_TOOL_CONTRACTS.get(tool_name, {}),
    }


def create_default_registry(
    config: ProviderConfig | None = None,
    *,
    video_context_store: VideoContextStore | None = None,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
    durable_task_service: DurableTaskService | None = None,
    enable_mcp_tools: bool = False,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_config_path: str | None = None,
    mcp_runner: MCPToolDiscoveryRunner | None = None,
    plugin_modules: Iterable[str] | None = None,
) -> ToolRegistry:
    config = config or ProviderConfig()
    mock_mode = config.provider_mode == "mock"
    resolved_mcp_server_configs = mcp_server_configs
    if (enable_mcp_tools or not mock_mode) and resolved_mcp_server_configs is None:
        from assistant_agent.mcp.config import load_mcp_server_configs_from_env

        resolved_mcp_server_configs = load_mcp_server_configs_from_env(config_path=mcp_config_path)
    if mock_mode:
        resolved_mcp_server_configs = []
    plugin_context = ToolPluginContext(
        config=config,
        mcp_server_configs=resolved_mcp_server_configs or [],
        mcp_runner=mcp_runner,
        video_context_store=video_context_store,
        realtime_video_memory_store=realtime_video_memory_store,
        durable_task_service=durable_task_service,
    )
    module_names = (
        configured_plugin_modules_from_env()
        if plugin_modules is None
        else normalize_configured_plugin_modules(plugin_modules)
    )
    assembly = assemble_tool_plugins(
        plugin_context,
        builtin_plugins=default_tool_plugins(),
        configured_module_names=module_names,
    )
    contributions = list(assembly.contributions)
    sources = list(assembly.report.sources)
    issues = list(assembly.report.issues)
    if not mock_mode and (enable_mcp_tools or mcp_server_configs is not None):
        from assistant_agent.mcp.registration import discover_configured_mcp_tools

        server_configs = resolved_mcp_server_configs or []
        discovered, mcp_summary = discover_configured_mcp_tools(
            server_configs,
            runner=mcp_runner,
        )
        for server in server_configs:
            sources.append(
                ToolPluginSourceRecord(
                    source_type="mcp",
                    source_ref=server.server_name,
                    trusted=True,
                )
            )
        for item in discovered:
            contributions.append(
                ToolContribution(
                    tool=item.tool,
                    registration=ToolRegistrationRecord(
                        tool_name=item.tool.name,
                        plugin_id=f"mcp.{item.server_name}",
                        plugin_version="1",
                        source_type="mcp",
                        source_ref=item.server_name,
                    ),
                )
            )
        issues.extend(
            ToolPluginLoadIssue(
                code="mcp_discovery_issue",
                message=message,
                source_ref="mcp",
            )
            for message in mcp_summary.issues
        )
    registry = ToolRegistry()
    try:
        registry.register_many(contributions)
    except Exception as exc:
        raise ToolPluginAssemblyError(
            ToolPluginAssemblyReport(
                sources=sources,
                issues=[
                    *issues,
                    ToolPluginLoadIssue(
                        code="atomic_registration_failed",
                        message=sanitize_error_message(exc),
                        source_ref="tool_registry",
                    ),
                ],
            )
        ) from exc
    report = ToolPluginAssemblyReport(
        sources=sources,
        registrations=[item.registration for item in contributions],
        issues=issues,
    )
    registry.seal(assembly_report=report)
    return registry


def create_realtime_video_observation_registry(
    config: ProviderConfig | None = None,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> ToolRegistry:
    """Create the governed realtime observer registry with one visual tool."""

    config = config or ProviderConfig()
    registry = ToolRegistry()
    tool = build_realtime_video_observation_tool(
        config,
        realtime_video_memory_store=realtime_video_memory_store,
    )
    registry.register(
        tool,
        ToolRegistrationRecord(
            tool_name=tool.name,
            plugin_id="vision.realtime_observer",
            plugin_version="1",
            source_type="realtime_observer",
            source_ref="assistant_agent.tools.plugins.vision.plugin",
        ),
    )
    registry.seal()
    return registry
