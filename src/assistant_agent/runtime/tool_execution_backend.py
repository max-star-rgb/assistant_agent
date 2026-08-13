"""Governed Tool invocation backends selected by trusted composition roots."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry


class ToolExecutionBackend(Protocol):
    """Invoke a validated Tool call without owning its runtime lifecycle."""

    def run(
        self,
        registry: ToolRegistry,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolContext,
    ) -> ToolResult: ...


class RegistryExecutionBackend:
    """Default backend that invokes the Tool registered for production."""

    def run(
        self,
        registry: ToolRegistry,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        return registry.run(tool_name, tool_input, context)
