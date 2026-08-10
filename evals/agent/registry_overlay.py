"""Production Tool Registry overlays for Agent eval Environments."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from assistant_agent.tools.base import Tool
from assistant_agent.tools.registry import ToolRegistry


DependencyMode = Literal["live", "controlled_replacement"]


@dataclass(frozen=True)
class EvalToolReplacement:
    """One Task-owned execution replacement for an existing production Tool."""

    tool_name: str
    tool: Tool
    reason: str
    source_ref: str


class EvalToolProvenance(BaseModel):
    """Safe dependency provenance retained outside the production Registry."""

    dependency_mode: DependencyMode
    production_source_type: str
    production_source_ref: str
    replacement_source_ref: str | None = None
    replacement_reason: str | None = None


@dataclass(frozen=True)
class EvalRegistryAssembly:
    """A sealed eval Registry paired with per-Tool dependency provenance."""

    registry: ToolRegistry
    provenance: dict[str, EvalToolProvenance]


def apply_tool_replacements(
    production_registry: ToolRegistry,
    replacements: Iterable[EvalToolReplacement],
) -> EvalRegistryAssembly:
    """Clone a sealed production Registry and atomically replace exact Tools."""

    if not production_registry.sealed:
        raise ValueError("production registry must be sealed")
    pending = list(replacements)
    names = [item.tool_name for item in pending]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(f"duplicate replacement tools: {duplicate_names}")
    production_names = set(production_registry.list())
    unknown_names = sorted(set(names) - production_names)
    if unknown_names:
        raise ValueError(f"unknown production tools: {unknown_names}")

    replacement_by_name: dict[str, EvalToolReplacement] = {}
    for item in pending:
        if not item.reason.strip():
            raise ValueError("replacement reason must be non-empty")
        if not item.source_ref.strip():
            raise ValueError("replacement source_ref must be non-empty")
        if item.tool.name != item.tool_name:
            raise ValueError(
                f"declared name {item.tool_name!r} does not match replacement "
                f"tool name {item.tool.name!r}"
            )
        if production_registry.get_spec(item.tool_name) != ToolRegistry._tool_spec(
            item.tool
        ):
            raise ValueError(
                f"replacement for {item.tool_name!r} changes ToolSpec"
            )
        replacement_by_name[item.tool_name] = item

    registry = ToolRegistry()
    provenance: dict[str, EvalToolProvenance] = {}
    for name in production_registry.list():
        registration = production_registry.registration_record(name)
        replacement = replacement_by_name.get(name)
        registry.register(
            replacement.tool if replacement is not None else production_registry.get(name),
            registration,
        )
        provenance[name] = EvalToolProvenance(
            dependency_mode=(
                "controlled_replacement" if replacement is not None else "live"
            ),
            production_source_type=registration.source_type,
            production_source_ref=registration.source_ref,
            replacement_source_ref=(
                replacement.source_ref if replacement is not None else None
            ),
            replacement_reason=(
                replacement.reason if replacement is not None else None
            ),
        )
    registry.seal()
    return EvalRegistryAssembly(registry=registry, provenance=provenance)
