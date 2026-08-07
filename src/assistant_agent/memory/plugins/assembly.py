"""Fail-closed assembly for explicitly configured Memory Plugin factories."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from assistant_agent.memory.plugins.config import (
    MEMORY_PLUGIN_EXPORT,
    MEMORY_PLUGIN_SLOT_MAX_LENGTH,
    MemoryPluginEntryConfig,
    MemoryPluginsConfig,
)
from assistant_agent.memory.plugins.contracts import (
    MemoryPlugin,
    MemoryPluginBuildContext,
    MemoryPluginDescriptor,
    MemoryPluginFactory,
    MemoryPluginIssue,
)
from assistant_agent.memory.plugins.registry import (
    MemoryPluginAssemblyReport,
    MemoryPluginRegistrationRecord,
    MemoryPluginRegistry,
)


class MemoryPluginAssemblyError(RuntimeError):
    """Raised when startup cannot seal a safe active Memory Plugin."""

    def __init__(self, report: MemoryPluginAssemblyReport) -> None:
        self.report = report
        codes = ",".join(issue.code for issue in report.issues)
        super().__init__(f"memory_plugin_assembly_failed:{codes}")


@dataclass(frozen=True)
class _Candidate:
    descriptor: MemoryPluginDescriptor
    factory: MemoryPluginFactory
    entry: MemoryPluginEntryConfig
    source: str


def assemble_memory_plugins(
    *,
    config: MemoryPluginsConfig,
    builtin_factories: Iterable[MemoryPluginFactory],
    build_context: MemoryPluginBuildContext,
) -> MemoryPluginRegistry:
    """Validate every declared factory, then construct only the selected slot."""

    if not _is_valid_memory_plugin_slot(config.slot):
        _fail(config.slot, "memory_plugin_slot_invalid")
    builtin_factories = tuple(builtin_factories)
    candidates: list[_Candidate] = []
    issues: list[MemoryPluginIssue] = []
    for factory in builtin_factories:
        candidate = _builtin_candidate(factory, issues)
        if candidate is not None:
            candidates.append(candidate)
    for configured_id, entry in config.plugins.items():
        candidate = _module_candidate(configured_id, entry, issues)
        if candidate is not None:
            candidates.append(candidate)

    _append_duplicate_issues(candidates, issues)
    for candidate in candidates:
        _validate_candidate_config(candidate, issues)
    if issues:
        _fail(config.slot, *issues)

    active_candidates = [
        candidate
        for candidate in candidates
        if candidate.descriptor.plugin_id == config.slot
    ]
    if not active_candidates:
        _fail(config.slot, "memory_plugin_slot_unknown")
    active_candidate = active_candidates[0]
    if not active_candidate.entry.enabled:
        _fail(config.slot, "memory_plugin_slot_disabled")

    try:
        plugin = active_candidate.factory.build(
            build_context,
            active_candidate.factory.config_model.model_validate(active_candidate.entry.config),
        )
    except Exception:
        _fail(config.slot, "memory_plugin_build_failed")
    try:
        plugin_descriptor_value = getattr(plugin, "descriptor", None)
    except Exception:
        _fail(config.slot, "memory_plugin_descriptor_invalid")
    plugin_descriptor = _validated_descriptor(plugin_descriptor_value)
    if plugin_descriptor is None:
        _fail(config.slot, "memory_plugin_descriptor_invalid")
    if plugin_descriptor != active_candidate.descriptor:
        _fail(config.slot, "memory_plugin_descriptor_mismatch")

    records = [
        MemoryPluginRegistrationRecord(
            descriptor=candidate.descriptor,
            source=candidate.source,
            enabled=candidate.entry.enabled,
            active=candidate is active_candidate,
        )
        for candidate in candidates
    ]
    return MemoryPluginRegistry(
        records,
        plugin,
        validated_active_descriptor=plugin_descriptor,
    )


def _builtin_candidate(
    factory: object,
    issues: list[MemoryPluginIssue],
) -> _Candidate | None:
    descriptor = _validate_factory(factory, issues)
    if descriptor is None:
        return None
    return _Candidate(
        descriptor=descriptor,
        factory=factory,
        entry=MemoryPluginEntryConfig(module="builtin"),
        source=f"builtin:{descriptor.plugin_id}",
    )


def _module_candidate(
    configured_id: str,
    entry: MemoryPluginEntryConfig,
    issues: list[MemoryPluginIssue],
) -> _Candidate | None:
    try:
        module = importlib.import_module(entry.module)
    except Exception:
        issues.append(_issue("memory_plugin_module_import_failed"))
        return None
    if not hasattr(module, MEMORY_PLUGIN_EXPORT):
        issues.append(_issue("memory_plugin_export_missing"))
        return None
    factory = getattr(module, MEMORY_PLUGIN_EXPORT)
    descriptor = _validate_factory(factory, issues)
    if descriptor is None:
        return None
    if descriptor.plugin_id != configured_id:
        issues.append(_issue("memory_plugin_descriptor_mismatch"))
        return None
    return _Candidate(
        descriptor=descriptor,
        factory=factory,
        entry=entry,
        source=f"module:{entry.module}",
    )


def _validate_factory(
    factory: object,
    issues: list[MemoryPluginIssue],
) -> MemoryPluginDescriptor | None:
    try:
        descriptor_value = getattr(factory, "descriptor", None)
    except Exception:
        issues.append(_issue("memory_plugin_descriptor_invalid"))
        return None
    descriptor = _validated_descriptor(descriptor_value)
    config_model = getattr(factory, "config_model", None)
    build = getattr(factory, "build", None)
    if (
        descriptor is None
        or not isinstance(config_model, type)
        or not issubclass(config_model, BaseModel)
        or not callable(build)
    ):
        issues.append(
            _issue(
                "memory_plugin_descriptor_invalid"
                if descriptor is None
                else "memory_plugin_factory_invalid"
            )
        )
        return None
    return descriptor


def _validate_candidate_config(
    candidate: _Candidate,
    issues: list[MemoryPluginIssue],
) -> None:
    try:
        candidate.factory.config_model.model_validate(candidate.entry.config)
    except Exception:
        issues.append(_issue("memory_plugin_config_invalid"))


def _append_duplicate_issues(
    candidates: list[_Candidate],
    issues: list[MemoryPluginIssue],
) -> None:
    seen: set[str] = set()
    for candidate in candidates:
        plugin_id = candidate.descriptor.plugin_id
        if plugin_id in seen:
            issues.append(_issue("memory_plugin_duplicate_id"))
        seen.add(plugin_id)


def _validated_descriptor(value: object) -> MemoryPluginDescriptor | None:
    if not isinstance(value, MemoryPluginDescriptor):
        return None
    try:
        return MemoryPluginDescriptor.model_validate(value.model_dump(mode="python"))
    except Exception:
        return None


def _issue(code: str) -> MemoryPluginIssue:
    return MemoryPluginIssue(code=code, message=code, recoverable=False)


def _fail(
    active_slot: object,
    *issues: str | MemoryPluginIssue,
) -> None:
    normalized = tuple(
        issue if isinstance(issue, MemoryPluginIssue) else _issue(issue)
        for issue in issues
    )
    raise MemoryPluginAssemblyError(
        MemoryPluginAssemblyReport(
            active_slot=(
                active_slot
                if _is_valid_memory_plugin_slot(active_slot)
                else "invalid-memory-plugin-slot"
            ),
            issues=normalized,
        )
    ) from None


def _is_valid_memory_plugin_slot(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MEMORY_PLUGIN_SLOT_MAX_LENGTH
    )
