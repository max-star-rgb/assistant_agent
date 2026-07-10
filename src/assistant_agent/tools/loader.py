"""Explicit loader for repo-local or user-local Python tools."""

from __future__ import annotations

import importlib
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.tools.registry import ToolRegistry


class LocalToolLoadIssue(BaseModel):
    """Non-fatal issue found while loading local tools."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    module: str = ""
    tool_name: str | None = None


class LocalToolLoadResult(BaseModel):
    """Explicitly loaded tools plus load/validation issues."""

    tools: list[Any] = Field(default_factory=list)
    issues: list[LocalToolLoadIssue] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


def load_local_tools(module_names: list[str]) -> LocalToolLoadResult:
    """Load local tool objects from explicit module names."""

    tools: list[Any] = []
    issues: list[LocalToolLoadIssue] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            issues.append(
                LocalToolLoadIssue(
                    code="import_failed",
                    message=f"Could not import module: {exc}",
                    module=module_name,
                )
            )
            continue
        module_tools = getattr(module, "__assistant_tools__", None)
        if not isinstance(module_tools, list):
            issues.append(
                LocalToolLoadIssue(
                    code="missing_tool_list",
                    message="Module must expose __assistant_tools__ as a list.",
                    module=module_name,
                )
            )
            continue
        for candidate in module_tools:
            issue = _validate_tool_shape(candidate, module_name=module_name)
            if issue is not None:
                issues.append(issue)
                continue
            tools.append(candidate)
    return LocalToolLoadResult(tools=tools, issues=issues)


def register_local_tools(registry: ToolRegistry, tools: list[Any]) -> None:
    """Register explicit local tools through ToolRegistry."""

    for local_tool in tools:
        registry.register(local_tool)


def _validate_tool_shape(tool: Any, *, module_name: str) -> LocalToolLoadIssue | None:
    tool_name = getattr(tool, "name", None)
    if not isinstance(tool_name, str) or not tool_name:
        return LocalToolLoadIssue(
            code="invalid_tool",
            message="Local tool must expose a non-empty name.",
            module=module_name,
        )
    for attr in ("description", "input_schema", "run"):
        if not hasattr(tool, attr):
            return LocalToolLoadIssue(
                code="invalid_tool",
                message=f"Local tool {tool_name} is missing {attr}.",
                module=module_name,
                tool_name=tool_name,
            )
    return None
