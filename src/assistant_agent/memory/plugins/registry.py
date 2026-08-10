"""Sealed inventory for the one active Assistant Memory Plugin."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.memory.plugins.config import (
    MEMORY_PLUGIN_REGISTRATION_SOURCE_MAX_LENGTH,
)
from assistant_agent.memory.plugins.contracts import (
    MemoryPlugin,
    MemoryPluginDescriptor,
    MemoryPluginIssue,
)


class _FrozenAssemblyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryPluginRegistrationRecord(_FrozenAssemblyModel):
    descriptor: MemoryPluginDescriptor
    source: str = Field(
        min_length=1,
        max_length=MEMORY_PLUGIN_REGISTRATION_SOURCE_MAX_LENGTH,
    )
    enabled: bool
    active: bool = False


class MemoryPluginAssemblyReport(_FrozenAssemblyModel):
    active_slot: str = Field(min_length=1, max_length=128)
    records: tuple[MemoryPluginRegistrationRecord, ...] = ()
    issues: tuple[MemoryPluginIssue, ...] = ()


class MemoryPluginRegistryError(ValueError):
    """Stable, sanitized failure raised while sealing a Registry."""

    def __init__(self, assembly_issue_code: str) -> None:
        self.assembly_issue_code = assembly_issue_code
        super().__init__("memory_plugin_registry_invalid")


class MemoryPluginRegistry:
    """An immutable inventory containing exactly one constructed plugin."""

    __slots__ = ("_active_plugin", "_assembly_report", "_generation")

    def __init__(
        self,
        records: Sequence[MemoryPluginRegistrationRecord],
        active_plugin: MemoryPlugin,
    ) -> None:
        sealed_record_list: list[MemoryPluginRegistrationRecord] = []
        seal_issue_code: str | None = None
        try:
            for record in records:
                sealed_record, record_issue_code = _validated_record(record)
                if record_issue_code is not None:
                    seal_issue_code = record_issue_code
                    break
                if sealed_record is None:
                    seal_issue_code = "memory_plugin_registry_invalid"
                    break
                sealed_record_list.append(sealed_record)
        except Exception:
            seal_issue_code = "memory_plugin_registry_invalid"
        if seal_issue_code is not None:
            raise MemoryPluginRegistryError(seal_issue_code) from None
        sealed_records = tuple(sealed_record_list)

        plugin_ids: set[str] = set()
        for record in sealed_records:
            plugin_id = record.descriptor.plugin_id
            if plugin_id in plugin_ids:
                raise MemoryPluginRegistryError(
                    "memory_plugin_registry_invalid"
                ) from None
            plugin_ids.add(plugin_id)

        active_records = [record for record in sealed_records if record.active]
        if len(active_records) != 1 or not active_records[0].enabled:
            raise MemoryPluginRegistryError(
                "memory_plugin_registry_invalid"
            ) from None

        active_descriptor_value: object = None
        descriptor_getter_failed = False
        try:
            active_descriptor_value = getattr(active_plugin, "descriptor")
        except Exception:
            descriptor_getter_failed = True
        if descriptor_getter_failed:
            raise MemoryPluginRegistryError(
                "memory_plugin_descriptor_invalid"
            ) from None
        active_descriptor = _validated_descriptor(active_descriptor_value)
        if active_descriptor is None:
            raise MemoryPluginRegistryError(
                "memory_plugin_descriptor_invalid"
            ) from None
        if active_records[0].descriptor != active_descriptor:
            raise MemoryPluginRegistryError(
                "memory_plugin_descriptor_mismatch"
            ) from None

        assembly_report: MemoryPluginAssemblyReport | None = None
        report_failed = False
        try:
            assembly_report = MemoryPluginAssemblyReport(
                active_slot=active_records[0].descriptor.plugin_id,
                records=sealed_records,
            )
        except Exception:
            report_failed = True
        if report_failed or assembly_report is None:
            raise MemoryPluginRegistryError(
                "memory_plugin_registry_invalid"
            ) from None

        generation: object = None
        generation_failed = False
        try:
            generation = _generation_for(assembly_report)
        except Exception:
            generation_failed = True
        if generation_failed or not isinstance(generation, str):
            raise MemoryPluginRegistryError(
                "memory_plugin_registry_invalid"
            ) from None

        self._active_plugin = active_plugin
        self._assembly_report = assembly_report
        self._generation = generation

    @property
    def active_plugin(self) -> MemoryPlugin:
        return self._active_plugin

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def assembly_report(self) -> MemoryPluginAssemblyReport:
        return self._assembly_report.model_copy(deep=True)


def _validated_record(
    value: object,
) -> tuple[MemoryPluginRegistrationRecord | None, str | None]:
    if not isinstance(value, MemoryPluginRegistrationRecord):
        return None, "memory_plugin_registry_invalid"
    descriptor_value: object = None
    descriptor_getter_failed = False
    try:
        descriptor_value = getattr(value, "descriptor")
    except Exception:
        descriptor_getter_failed = True
    if descriptor_getter_failed:
        return None, "memory_plugin_descriptor_invalid"
    descriptor = _validated_descriptor(descriptor_value)
    if descriptor is None:
        return None, "memory_plugin_descriptor_invalid"
    sealed_record: MemoryPluginRegistrationRecord | None = None
    record_failed = False
    try:
        sealed_record = MemoryPluginRegistrationRecord(
            descriptor=descriptor,
            source=value.source,
            enabled=value.enabled,
            active=value.active,
        )
    except Exception:
        record_failed = True
    if record_failed or sealed_record is None:
        return None, "memory_plugin_registry_invalid"
    return sealed_record, None


def _validated_descriptor(value: object) -> MemoryPluginDescriptor | None:
    if not isinstance(value, MemoryPluginDescriptor):
        return None
    try:
        return MemoryPluginDescriptor.model_validate(
            value.model_dump(mode="python")
        )
    except Exception:
        return None


def _generation_for(report: MemoryPluginAssemblyReport) -> str:
    payload = {
        "active_slot": report.active_slot,
        "plugins": [
            {
                "descriptor": _canonicalize(
                    record.descriptor.model_dump(mode="python")
                ),
                "source": record.source,
            }
            for record in sorted(
                report.records,
                key=lambda record: (record.descriptor.plugin_id, record.source),
            )
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda item: item[0])
        }
    if isinstance(value, (set, frozenset)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return value
