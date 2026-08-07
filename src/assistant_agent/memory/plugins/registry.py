"""Sealed inventory for the one active Assistant Memory Plugin."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.memory.plugins.contracts import (
    MemoryPlugin,
    MemoryPluginDescriptor,
    MemoryPluginIssue,
)


class _FrozenAssemblyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryPluginRegistrationRecord(_FrozenAssemblyModel):
    descriptor: MemoryPluginDescriptor
    source: str = Field(min_length=1, max_length=512)
    enabled: bool
    active: bool = False


class MemoryPluginAssemblyReport(_FrozenAssemblyModel):
    active_slot: str = Field(min_length=1, max_length=128)
    records: tuple[MemoryPluginRegistrationRecord, ...] = ()
    issues: tuple[MemoryPluginIssue, ...] = ()


class MemoryPluginRegistry:
    """An immutable inventory containing exactly one constructed plugin."""

    __slots__ = ("_active_plugin", "_assembly_report", "_generation")

    def __init__(
        self,
        records: Sequence[MemoryPluginRegistrationRecord],
        active_plugin: MemoryPlugin,
    ) -> None:
        sealed_records = tuple(records)
        active_records = [record for record in sealed_records if record.active]
        if len(active_records) != 1 or active_records[0].descriptor != active_plugin.descriptor:
            raise ValueError("memory_plugin_registry_invalid")
        self._active_plugin = active_plugin
        self._assembly_report = MemoryPluginAssemblyReport(
            active_slot=active_records[0].descriptor.plugin_id,
            records=sealed_records,
        )
        self._generation = _generation_for(self._assembly_report)

    @property
    def active_plugin(self) -> MemoryPlugin:
        return self._active_plugin

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def assembly_report(self) -> MemoryPluginAssemblyReport:
        return self._assembly_report


def _generation_for(report: MemoryPluginAssemblyReport) -> str:
    payload = {
        "active_slot": report.active_slot,
        "plugins": [
            {
                "descriptor": record.descriptor.model_dump(mode="json"),
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
