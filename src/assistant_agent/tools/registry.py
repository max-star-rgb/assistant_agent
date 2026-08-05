"""Tool registry and default tool registration."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Protocol

from pydantic import BaseModel

from assistant_agent.tools.models import ToolResult, ToolSpec
from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.tools.input_binding import (
    llm_forbidden_input_fields,
    validate_tool_input_contract,
)
from assistant_agent.tools.plugins.contracts import (
    ToolPluginAssemblyReport,
    ToolRegistrationRecord,
)


_REGISTERED_TOOL_CONTRACTS: dict[str, dict[str, Any]] = {}


class _ToolContribution(Protocol):
    tool: Tool
    registration: ToolRegistrationRecord


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
        }

    def register_many(self, contributions: Iterable[_ToolContribution]) -> None:
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

    def notify_run_terminal(self, run_id: str, status: str) -> list[str]:
        """Best-effort notify optional Tool lifecycle owners after one run ends."""

        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid run terminal status")
        issues: list[str] = []
        for name in sorted(self._tools):
            callback = getattr(self._tools[name], "on_run_terminal", None)
            if not callable(callback):
                continue
            try:
                callback(run_id, status)
            except Exception:
                issues.append(name)
        return issues

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

    @staticmethod
    def _tool_spec(tool: Tool) -> ToolSpec:
        validate_tool_input_contract(tool)
        return ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=_schema_to_dict(
                tool.input_schema,
                hidden_fields=llm_forbidden_input_fields(tool),
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
        "requires_media",
        "media_scope",
        "repeat_policy",
    )
    return {name: getattr(tool, name) for name in fields if hasattr(tool, name)}


def tool_contract_fields(tool_name: str) -> dict[str, Any]:
    """Return the last registered contract for legacy realtime projections."""

    return {
        "category": "dangerous",
        **_REGISTERED_TOOL_CONTRACTS.get(tool_name, {}),
    }
