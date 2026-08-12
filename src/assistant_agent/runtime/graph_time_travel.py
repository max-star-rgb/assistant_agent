"""Safe product contracts for native graph time-travel capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GraphCheckpointSelector(_StrictModel):
    """Opaque reference to one re-entry-safe native graph checkpoint."""

    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")


class GraphCheckpointSummary(_StrictModel):
    """Bounded product-safe projection of one native state snapshot."""

    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")
    created_at: datetime
    status: Literal["running", "waiting_user", "completed", "failed", "cancelled"]
    next_nodes: tuple[str, ...]
    has_interrupt: bool
    graph_version: str
    state_schema_version: int


def graph_history_ref(
    *,
    thread_id: str,
    snapshot_config: Mapping[str, Any],
) -> str:
    """Derive an opaque selector without retaining a process-local lookup map."""

    canonical = json.dumps(
        {
            "domain": "assistant_graph_history_ref_v1",
            "thread_id": thread_id,
            "config": snapshot_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "ghr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "GraphCheckpointSelector",
    "GraphCheckpointSummary",
    "graph_history_ref",
]
