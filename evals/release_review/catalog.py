from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from assistant_agent.tools.models import ToolSpec
from assistant_agent.tools.registry import ToolRegistry


class ReleaseCatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: str
    tools: tuple[ToolSpec, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.tools)

    def require_tools(self, required: Iterable[str]) -> None:
        missing = sorted(set(required) - set(self.tool_names))
        if missing:
            raise ValueError(f"missing required tools: {', '.join(missing)}")


def build_catalog_snapshot(registry: ToolRegistry) -> ReleaseCatalogSnapshot:
    if not registry.sealed:
        raise ValueError("release review requires a sealed ToolRegistry")
    tools = tuple(sorted(registry.list_specs(), key=lambda spec: spec.name))
    canonical = json.dumps(
        [spec.model_dump(mode="json") for spec in tools],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    generation = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return ReleaseCatalogSnapshot(generation=generation, tools=tools)

