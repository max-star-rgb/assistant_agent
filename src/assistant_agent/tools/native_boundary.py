"""Native LangChain tool output and error boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt import ToolRuntime

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
    observation = result.model_observation or result.data or {"status": "succeeded"}
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
    except Exception as exc:
        raise ToolException(sanitize_error_message(exc)) from exc


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


def native_idempotency_key(runtime: ToolRuntime[AssistantRunContext]) -> str:
    """Return the idempotency key scoped to this native tool call."""

    thread_id = runtime.execution_info.thread_id or "thread"
    return f"native:{thread_id}:{runtime.tool_call_id or 'tool-call'}"
