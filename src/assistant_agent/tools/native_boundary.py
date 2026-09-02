"""Native LangChain tool output and error boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.tools import BaseTool, ToolException
from langgraph.prebuilt import ToolRuntime
from pydantic import ValidationError

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.providers.provider_errors import sanitize_error_message


def native_content_and_artifact(
    model_observation: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return (
        [{"type": "text", "text": json.dumps(dict(model_observation), ensure_ascii=False, sort_keys=True, indent=2)}],
        dict(artifact),
    )


def native_tool_exception(
    exc: BaseException,
    *,
    tool_name: str = "tool",
) -> ToolException:
    if isinstance(exc, ValidationError):
        return ToolException(_validation_error_message(tool_name, exc))
    return ToolException(sanitize_error_message(str(exc)))


def builtin_tool_metadata(
    *,
    availability: str | None = None,
) -> dict[str, Any]:
    """Return the metadata required for a built-in native tool."""

    metadata = {"source": "builtin"}
    if availability is not None:
        metadata["availability"] = availability
    return metadata


def configure_builtin_tool(
    tool: BaseTool,
    *,
    availability: str | None = None,
    bounded_expected_errors: bool = False,
    bounded_validation_errors: bool = False,
) -> BaseTool:
    """Apply standard metadata and the production ToolException policy."""

    tool.metadata = builtin_tool_metadata(
        availability=availability,
    )
    if bounded_expected_errors:
        tool.handle_tool_error = _bounded_tool_error
    if bounded_validation_errors:
        tool.handle_validation_error = lambda exc: _validation_error_message(
            tool.name, exc
        )
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
