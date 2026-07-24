"""Low-friction local tool declaration helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from assistant_agent.schemas.tools import ToolCategory, ToolMediaRequirement, ToolResult
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.tools.base import ToolContext


ToolHandler = Callable[[BaseModel, ToolContext], ToolResult | dict[str, Any]]


class DecoratedTool:
    """Tool object produced by the local @tool decorator."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: type[BaseModel],
        handler: ToolHandler,
        category: ToolCategory = "dangerous",
        requires_confirmation: bool = True,
        requires_media: list[ToolMediaRequirement] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = input_schema
        self.category = category
        self.requires_confirmation = requires_confirmation
        self.requires_media = list(requires_media or [])
        self._handler = handler

    def run(
        self,
        input: BaseModel | dict[str, Any],
        context: ToolContext | None = None,
    ) -> ToolResult:
        try:
            payload = (
                input
                if isinstance(input, self.input_schema)
                else self.input_schema.model_validate(input)
            )
            result = self._handler(payload, context or ToolContext())
        except ValidationError as exc:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Invalid input: {exc.errors()[0]['msg']}",
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=sanitize_error_message(exc),
            )
        if isinstance(result, ToolResult):
            return result
        return ToolResult(tool_name=self.name, success=True, data=dict(result))


def tool(
    *,
    name: str,
    description: str = "",
    input_schema: type[BaseModel],
    category: ToolCategory = "dangerous",
    requires_confirmation: bool = True,
    requires_media: list[ToolMediaRequirement] | None = None,
) -> Callable[[ToolHandler], DecoratedTool]:
    """Return a local tool object without registering it globally."""

    def decorate(handler: ToolHandler) -> DecoratedTool:
        return DecoratedTool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            category=category,
            requires_confirmation=requires_confirmation,
            requires_media=requires_media,
        )

    return decorate
