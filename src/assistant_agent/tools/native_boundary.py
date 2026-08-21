"""Native LangChain tool output and error boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, ToolException
from langgraph.prebuilt import ToolRuntime
from pydantic import ValidationError

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.tools.models import ToolCategory, ToolResult


def native_tool_response(
    tool_name: str,
    result: ToolResult,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project a successful result into LangChain content and artifact values."""

    if not result.success:
        raise ToolException(result.error or f"{tool_name} failed")
    observation = result.model_observation
    if observation is None:
        observation = result.data or {"status": "succeeded"}
    return (
        [
            {
                "type": "text",
                "text": json.dumps(
                    observation,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
            }
        ],
        dict(result.data or {}),
    )


def invoke_native_tool(
    tool_name: str,
    operation: Callable[[], ToolResult],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute a business operation with native ToolException semantics."""

    try:
        return native_tool_response(tool_name, operation())
    except ToolException:
        raise
    except ValidationError as exc:
        raise ToolException(_validation_error_message(tool_name, exc)) from exc
    except Exception as exc:
        raise ToolException(sanitize_error_message(str(exc))) from exc


def builtin_tool_metadata(
    effect: ToolCategory,
    *,
    availability: str | None = None,
) -> dict[str, str]:
    """Return the metadata required for a built-in native tool."""

    metadata = {"effect": effect, "source": "builtin"}
    if availability is not None:
        metadata["availability"] = availability
    return metadata


def configure_builtin_tool(
    tool: BaseTool,
    effect: ToolCategory,
    *,
    availability: str | None = None,
    bounded_expected_errors: bool = False,
) -> BaseTool:
    """Apply standard metadata and the production ToolException policy."""

    tool.metadata = builtin_tool_metadata(effect, availability=availability)
    if effect != "read" or bounded_expected_errors:
        tool.handle_tool_error = _bounded_tool_error
    return tool


def native_idempotency_key(runtime: ToolRuntime[AssistantRunContext]) -> str:
    """Return the idempotency key scoped to this native tool call."""

    thread_id = runtime.execution_info.thread_id or "thread"
    run_id = runtime.execution_info.run_id or "run"
    tool_call_id = runtime.tool_call_id or "tool-call"
    return f"native:{thread_id}:{run_id}:{tool_call_id}"


def _bounded_tool_error(exc: ToolException) -> str:
    """Return bounded model-visible content for an expected Tool failure."""

    return sanitize_error_message(str(exc))


def _validation_error_message(tool_name: str, exc: ValidationError) -> str:
    """Project one domain validation issue without echoing the raw input."""

    errors = exc.errors(include_url=False, include_input=False)
    if not errors:
        return f"Invalid input for {tool_name}."
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "request"
    message = str(first.get("msg") or "validation failed")
    return sanitize_error_message(
        f"Invalid input for {tool_name} at {location}: {message}"
    )
