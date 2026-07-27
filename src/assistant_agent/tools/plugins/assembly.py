"""Startup-only discovery and atomic assembly for in-process Tool plugins."""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from pydantic import BaseModel

from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.tools.base import Tool
from assistant_agent.tools.plugins.contracts import (
    LoadedToolPlugin,
    ToolPluginAssemblyReport,
    ToolPluginContext,
    ToolPluginDescriptor,
    ToolPluginLoadIssue,
    ToolPluginSourceRecord,
    ToolRegistrationRecord,
)


TOOL_PLUGIN_MODULES_ENV = "MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES"
_MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


@dataclass(frozen=True)
class ToolContribution:
    """One Tool plus the ownership record committed with it."""

    tool: Tool
    registration: ToolRegistrationRecord


@dataclass(frozen=True)
class ToolPluginAssemblyResult:
    """Validated contributions ready for one atomic Registry commit."""

    contributions: list[ToolContribution]
    report: ToolPluginAssemblyReport


class ToolPluginAssemblyError(RuntimeError):
    """Fail-closed startup error retaining only sanitized diagnostics."""

    def __init__(self, report: ToolPluginAssemblyReport) -> None:
        self.report = report
        details = "; ".join(
            f"{issue.code} ({issue.source_ref}): {issue.message}"
            for issue in report.issues
        )
        super().__init__(details or "Tool plugin assembly failed.")


def configured_plugin_modules_from_env(
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Parse the explicit operator allowlist, preserving first-seen order."""

    source = os.environ if env is None else env
    return normalize_configured_plugin_modules(
        source.get(TOOL_PLUGIN_MODULES_ENV, "").split(",")
    )


def normalize_configured_plugin_modules(values: Iterable[str]) -> list[str]:
    """Trim and de-duplicate an explicit module sequence."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        module_name = value.strip()
        if not module_name or module_name in seen:
            continue
        seen.add(module_name)
        result.append(module_name)
    return result


def assemble_tool_plugins(
    context: ToolPluginContext,
    *,
    builtin_plugins: Iterable[Any],
    configured_module_names: Iterable[str] = (),
) -> ToolPluginAssemblyResult:
    """Discover, build, and validate every plugin before Registry mutation."""

    loaded: list[LoadedToolPlugin] = []
    sources: list[ToolPluginSourceRecord] = []
    issues: list[ToolPluginLoadIssue] = []

    for plugin in builtin_plugins:
        descriptor = _descriptor_for(plugin, source_ref=plugin.__class__.__module__, issues=issues)
        if descriptor is None:
            continue
        source = ToolPluginSourceRecord(
            source_type="builtin",
            source_ref=plugin.__class__.__module__,
            trusted=True,
        )
        sources.append(source)
        loaded.append(LoadedToolPlugin(plugin=plugin, descriptor=descriptor, source=source))

    for module_name in configured_module_names:
        source = ToolPluginSourceRecord(
            source_type="configured_module",
            source_ref=module_name,
            trusted=True,
        )
        sources.append(source)
        if not _MODULE_NAME_PATTERN.fullmatch(module_name):
            issues.append(
                ToolPluginLoadIssue(
                    code="invalid_module_name",
                    message="Configured plugin module name is invalid.",
                    source_ref=module_name,
                )
            )
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            issues.append(
                ToolPluginLoadIssue(
                    code="plugin_import_failed",
                    message=sanitize_error_message(exc),
                    source_ref=module_name,
                )
            )
            continue
        plugin = getattr(module, "__assistant_tool_plugin__", None)
        if plugin is None:
            issues.append(
                ToolPluginLoadIssue(
                    code="missing_plugin_export",
                    message="Module must expose __assistant_tool_plugin__.",
                    source_ref=module_name,
                )
            )
            continue
        descriptor = _descriptor_for(plugin, source_ref=module_name, issues=issues)
        if descriptor is not None:
            loaded.append(LoadedToolPlugin(plugin=plugin, descriptor=descriptor, source=source))

    _append_duplicate_plugin_issues(loaded, issues)
    if issues:
        raise ToolPluginAssemblyError(
            ToolPluginAssemblyReport(sources=sources, issues=issues)
        )

    contributions: list[ToolContribution] = []
    seen_tool_names: dict[str, str] = {}
    for loaded_plugin in loaded:
        descriptor = loaded_plugin.descriptor
        try:
            built_tools = loaded_plugin.plugin.build_tools(context)
        except Exception as exc:
            issues.append(
                ToolPluginLoadIssue(
                    code="plugin_build_failed",
                    message=sanitize_error_message(exc),
                    source_ref=loaded_plugin.source.source_ref,
                    plugin_id=descriptor.plugin_id,
                )
            )
            continue
        if not isinstance(built_tools, list):
            issues.append(
                ToolPluginLoadIssue(
                    code="invalid_plugin_result",
                    message="build_tools() must return a list.",
                    source_ref=loaded_plugin.source.source_ref,
                    plugin_id=descriptor.plugin_id,
                )
            )
            continue
        plugin_contributions: list[ToolContribution] = []
        plugin_failed = False
        for tool in built_tools:
            tool_name = getattr(tool, "name", None)
            issue = _tool_shape_issue(
                tool,
                source_ref=loaded_plugin.source.source_ref,
                plugin_id=descriptor.plugin_id,
            )
            if issue is not None:
                issues.append(issue)
                plugin_failed = True
                continue
            assert isinstance(tool_name, str)
            owner = seen_tool_names.get(tool_name)
            if owner is not None:
                issues.append(
                    ToolPluginLoadIssue(
                        code="duplicate_tool_name",
                        message=f"Tool name is already contributed by plugin {owner}.",
                        source_ref=loaded_plugin.source.source_ref,
                        plugin_id=descriptor.plugin_id,
                        tool_name=tool_name,
                    )
                )
                plugin_failed = True
                continue
            seen_tool_names[tool_name] = descriptor.plugin_id
            plugin_contributions.append(
                ToolContribution(
                    tool=tool,
                    registration=ToolRegistrationRecord(
                        tool_name=tool_name,
                        plugin_id=descriptor.plugin_id,
                        plugin_version=descriptor.plugin_version,
                        source_type=loaded_plugin.source.source_type,
                        source_ref=loaded_plugin.source.source_ref,
                    ),
                )
            )
        if not plugin_failed:
            contributions.extend(plugin_contributions)

    if issues:
        raise ToolPluginAssemblyError(
            ToolPluginAssemblyReport(sources=sources, issues=issues)
        )
    registrations = [item.registration for item in contributions]
    return ToolPluginAssemblyResult(
        contributions=contributions,
        report=ToolPluginAssemblyReport(
            sources=sources,
            registrations=registrations,
        ),
    )


def _descriptor_for(
    plugin: Any,
    *,
    source_ref: str,
    issues: list[ToolPluginLoadIssue],
) -> ToolPluginDescriptor | None:
    if not callable(getattr(plugin, "build_tools", None)):
        issues.append(
            ToolPluginLoadIssue(
                code="invalid_plugin_shape",
                message="Plugin must expose build_tools(context).",
                source_ref=source_ref,
            )
        )
        return None
    try:
        return ToolPluginDescriptor.model_validate(getattr(plugin, "descriptor", None))
    except Exception as exc:
        issues.append(
            ToolPluginLoadIssue(
                code="invalid_plugin_descriptor",
                message=sanitize_error_message(exc),
                source_ref=source_ref,
            )
        )
        return None


def _append_duplicate_plugin_issues(
    loaded: list[LoadedToolPlugin],
    issues: list[ToolPluginLoadIssue],
) -> None:
    owners: dict[str, str] = {}
    for item in loaded:
        plugin_id = item.descriptor.plugin_id
        previous_source = owners.get(plugin_id)
        if previous_source is None:
            owners[plugin_id] = item.source.source_ref
            continue
        issues.append(
            ToolPluginLoadIssue(
                code="duplicate_plugin_id",
                message=f"Plugin id is already provided by {previous_source}.",
                source_ref=item.source.source_ref,
                plugin_id=plugin_id,
            )
        )


def _tool_shape_issue(
    tool: Any,
    *,
    source_ref: str,
    plugin_id: str,
) -> ToolPluginLoadIssue | None:
    tool_name = getattr(tool, "name", None)
    if not isinstance(tool_name, str) or not tool_name:
        return ToolPluginLoadIssue(
            code="invalid_tool",
            message="Tool must expose a non-empty name.",
            source_ref=source_ref,
            plugin_id=plugin_id,
        )
    if not isinstance(getattr(tool, "description", None), str):
        message = "Tool must expose a string description."
    elif not (
        isinstance(getattr(tool, "input_schema", None), type)
        and issubclass(tool.input_schema, BaseModel)
    ):
        message = "Tool input_schema must be a Pydantic BaseModel type."
    elif not callable(getattr(tool, "run", None)):
        message = "Tool must expose run(input, context)."
    else:
        return None
    return ToolPluginLoadIssue(
        code="invalid_tool",
        message=message,
        source_ref=source_ref,
        plugin_id=plugin_id,
        tool_name=tool_name,
    )
