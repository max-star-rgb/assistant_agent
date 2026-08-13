"""Thin composition value for graph-native long-term-memory nodes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.store.base import BaseStore


MemoryGraphNode = Callable[[Any, Any], Any]


@dataclass(frozen=True)
class MemoryNodeBundle:
    """Resources installed by the composition root; deliberately no behavior."""

    backend_id: str
    recall_node: MemoryGraphNode
    commit_node: MemoryGraphNode
    store: BaseStore | None = None
    aclose: Callable[[], Awaitable[None]] | None = None


__all__ = ["MemoryGraphNode", "MemoryNodeBundle"]
