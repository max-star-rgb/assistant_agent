"""Fail-closed assembly for explicitly configured Memory Plugin factories."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from pydantic import BaseModel

from assistant_agent.memory.plugins.config import (
    MEMORY_PLUGIN_EXPORT,
    MEMORY_PLUGIN_MODULE_SOURCE_PREFIX,
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
    MemoryPluginRegistryError,
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
    config_model: type[BaseModel]
    validate_config: Callable[[object], object]
    build: Callable[[MemoryPluginBuildContext, BaseModel], object]
    entry: MemoryPluginEntryConfig
    source: str
    validated_config: BaseModel | None = None


@dataclass(frozen=True)
class _FactorySnapshot:
    descriptor: MemoryPluginDescriptor
    config_model: type[BaseModel]
    validate_config: Callable[[object], object]
    build: Callable[[MemoryPluginBuildContext, BaseModel], object]


_MISSING_EXPORT = object()


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
    validated_candidates: list[_Candidate] = []
    for candidate in candidates:
        validated_config = _validate_candidate_config(candidate, issues)
        if validated_config is not None:
            validated_candidates.append(
                replace(candidate, validated_config=validated_config)
            )
    if issues:
        _fail(config.slot, *issues)
    candidates = validated_candidates

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
    if active_candidate.validated_config is None:
        _fail(config.slot, "memory_plugin_config_invalid")

    try:
        plugin = active_candidate.build(
            build_context,
            active_candidate.validated_config,
        )
    except Exception:
        _fail(config.slot, "memory_plugin_build_failed")
    try:
        records = [
            MemoryPluginRegistrationRecord(
                descriptor=candidate.descriptor,
                source=candidate.source,
                enabled=candidate.entry.enabled,
                active=candidate is active_candidate,
            )
            for candidate in candidates
        ]
        return MemoryPluginRegistry(records, plugin)
    except MemoryPluginRegistryError as exc:
        _fail(config.slot, exc.assembly_issue_code)
    except Exception:
        _fail(config.slot, "memory_plugin_registry_invalid")


def _builtin_candidate(
    factory: object,
    issues: list[MemoryPluginIssue],
) -> _Candidate | None:
    snapshot = _snapshot_factory(factory, issues)
    if snapshot is None:
        return None
    return _Candidate(
        descriptor=snapshot.descriptor,
        config_model=snapshot.config_model,
        validate_config=snapshot.validate_config,
        build=snapshot.build,
        entry=MemoryPluginEntryConfig(module="builtin"),
        source=f"builtin:{snapshot.descriptor.plugin_id}",
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
    try:
        factory = getattr(module, MEMORY_PLUGIN_EXPORT, _MISSING_EXPORT)
    except Exception:
        issues.append(_issue("memory_plugin_export_missing"))
        return None
    if factory is _MISSING_EXPORT:
        issues.append(_issue("memory_plugin_export_missing"))
        return None
    snapshot = _snapshot_factory(factory, issues)
    if snapshot is None:
        return None
    if snapshot.descriptor.plugin_id != configured_id:
        issues.append(_issue("memory_plugin_descriptor_mismatch"))
        return None
    return _Candidate(
        descriptor=snapshot.descriptor,
        config_model=snapshot.config_model,
        validate_config=snapshot.validate_config,
        build=snapshot.build,
        entry=entry,
        source=f"{MEMORY_PLUGIN_MODULE_SOURCE_PREFIX}{entry.module}",
    )


def _snapshot_factory(
    factory: object,
    issues: list[MemoryPluginIssue],
) -> _FactorySnapshot | None:
    try:
        descriptor_value = getattr(factory, "descriptor", None)
    except Exception:
        issues.append(_issue("memory_plugin_descriptor_invalid"))
        return None
    descriptor = _validated_descriptor(descriptor_value)
    if descriptor is None:
        issues.append(_issue("memory_plugin_descriptor_invalid"))
        return None
    try:
        config_model = getattr(factory, "config_model", None)
        build = getattr(factory, "build", None)
    except Exception:
        issues.append(_issue("memory_plugin_factory_invalid"))
        return None
    if (
        not isinstance(config_model, type)
        or not issubclass(config_model, BaseModel)
        or not callable(build)
    ):
        issues.append(_issue("memory_plugin_factory_invalid"))
        return None
    try:
        validate_config = getattr(config_model, "model_validate", None)
    except Exception:
        issues.append(_issue("memory_plugin_config_invalid"))
        return None
    if not callable(validate_config):
        issues.append(_issue("memory_plugin_config_invalid"))
        return None
    return _FactorySnapshot(
        descriptor=descriptor,
        config_model=config_model,
        validate_config=validate_config,
        build=build,
    )


def _validate_candidate_config(
    candidate: _Candidate,
    issues: list[MemoryPluginIssue],
) -> BaseModel | None:
    try:
        validated_config = candidate.validate_config(candidate.entry.config)
    except Exception:
        issues.append(_issue("memory_plugin_config_invalid"))
        return None
    if not isinstance(validated_config, BaseModel):
        issues.append(_issue("memory_plugin_config_invalid"))
        return None
    return validated_config


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
