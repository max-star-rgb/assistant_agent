"""Official MCP SDK-backed client runner for external MCP tools."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

import anyio
from pydantic import BaseModel

from assistant_agent.mcp.adapter import (
    MCPToolDefinition,
    MCPToolRunner,
    namespaced_mcp_tool_name,
)
from assistant_agent.mcp.config import (
    MCPServerConfig,
    MCPToolAdapterConfig,
    resolve_mcp_server_env,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.providers.provider_errors import (
    ProviderSafetyPolicy,
    sanitize_error_detail,
    sanitize_error_message,
)

_T = TypeVar("_T")
_PROXY_ENVIRONMENT_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
# Stable adapters may need to normalize hourly MCP payloads before projecting a
# compact model observation. This content remains runtime-only and sanitized;
# default traces still receive the tool-owned summary rather than this payload.
_MCP_CONTENT_POLICY = ProviderSafetyPolicy(max_message_chars=256_000, max_detail_chars=256_000)


class SdkMCPClientRunner(MCPToolRunner):
    """Discover and execute MCP tools through the official Python SDK."""

    def __init__(self, servers: list[MCPServerConfig]) -> None:
        self._servers = {server.server_name: server for server in servers}

    def list_tools(self, *, server: MCPServerConfig) -> list[MCPToolDefinition]:
        return _run_async_from_sync(lambda: self._list_tools(server))

    def run_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        server = self._servers.get(server_name)
        namespaced_tool_name = (
            namespaced_mcp_tool_name(server.adapter_config(), tool_name)
            if server is not None
            else namespaced_mcp_tool_name(
                MCPToolAdapterConfig(server_name=server_name or "unknown"),
                tool_name or "unknown",
            )
        )
        if server is None:
            return ToolResult(
                tool_name=namespaced_tool_name,
                success=False,
                error=sanitize_error_message(f"MCP server is not configured: {server_name}"),
            )
        if not server.adapter_config().is_allowed(tool_name):
            return ToolResult(
                tool_name=namespaced_tool_name,
                success=False,
                error=sanitize_error_message(f"MCP tool is not allowlisted: {tool_name}"),
            )
        try:
            return _run_async_from_sync(
                lambda: self._run_tool(
                    server=server,
                    tool_name=tool_name,
                    namespaced_tool_name=namespaced_tool_name,
                    tool_input=tool_input,
                )
            )
        except Exception as exc:
            return ToolResult(
                tool_name=namespaced_tool_name,
                success=False,
                error=_mcp_execution_error_message(exc),
            )

    async def _list_tools(self, server: MCPServerConfig) -> list[MCPToolDefinition]:
        deadline = anyio.current_time() + server.timeout_seconds
        async with _sdk_session(server, deadline=deadline) as session:
            with anyio.fail_after(_remaining_timeout_seconds(deadline)):
                response = await session.list_tools()
        definitions: list[MCPToolDefinition] = []
        for tool in response.tools:
            tool_payload = _dump_sdk_value(tool)
            if not isinstance(tool_payload, dict):
                continue
            name = tool_payload.get("name")
            if not isinstance(name, str) or not name:
                continue
            input_schema = (
                tool_payload.get("inputSchema")
                or tool_payload.get("input_schema")
                or tool_payload.get("inputSchema".lower())
                or {}
            )
            if not isinstance(input_schema, dict):
                input_schema = {}
            definitions.append(
                MCPToolDefinition(
                    name=name,
                    description=str(tool_payload.get("description") or ""),
                    input_schema=input_schema,
                )
            )
        return definitions

    async def _run_tool(
        self,
        *,
        server: MCPServerConfig,
        tool_name: str,
        namespaced_tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        deadline = anyio.current_time() + server.timeout_seconds
        async with _sdk_session(server, deadline=deadline) as session:
            response = await session.call_tool(
                tool_name,
                arguments=tool_input,
                read_timeout_seconds=timedelta(
                    seconds=_remaining_timeout_seconds(deadline)
                ),
            )
        return _tool_result_from_sdk_response(
            server=server,
            tool_name=tool_name,
            namespaced_tool_name=namespaced_tool_name,
            response=response,
        )


def _run_async_from_sync(factory: Callable[[], Awaitable[_T]]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: dict[str, _T] = {}
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            result["value"] = asyncio.run(factory())
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            errors.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result["value"]


def _sdk_session(server: MCPServerConfig, *, deadline: float):
    from contextlib import asynccontextmanager

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def _session():
        command = server.command[0]
        args = server.command[1:]
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=_mcp_subprocess_environment(server.env),
            cwd=server.cwd,
        )
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(server_params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    with anyio.fail_after(_remaining_timeout_seconds(deadline)):
                        await session.initialize()
                    yield session

    return _session()


def _remaining_timeout_seconds(deadline: float) -> float:
    remaining = deadline - anyio.current_time()
    if remaining <= 0:
        raise TimeoutError("MCP operation timed out.")
    return remaining


def _mcp_execution_error_message(exc: BaseException) -> str:
    leaves = _exception_leaves(exc)
    for leaf in leaves:
        message = sanitize_error_message(str(leaf) or leaf)
        lowered = message.lower()
        if "timeout" in lowered or "timed out" in lowered:
            if lowered.startswith("provider_timeout:"):
                return message
            return f"provider_timeout: {message}"
    selected = leaves[0] if leaves else exc
    return sanitize_error_message(str(selected) or selected)


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [
            leaf
            for nested in exc.exceptions
            for leaf in _exception_leaves(nested)
        ]
    return [exc]


def _mcp_subprocess_environment(server_env: dict[str, str]) -> dict[str, str] | None:
    """Pass operator proxy settings through the MCP SDK's restricted environment."""
    has_protocol_proxy = any(
        os.environ.get(key)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    )
    environment = {
        key: value
        for key in _PROXY_ENVIRONMENT_VARIABLES
        if not (has_protocol_proxy and key in {"ALL_PROXY", "all_proxy"})
        if (value := os.environ.get(key)) is not None
    }
    environment.update(resolve_mcp_server_env(server_env))
    return environment or None


def _tool_result_from_sdk_response(
    *,
    server: MCPServerConfig,
    tool_name: str,
    namespaced_tool_name: str,
    response: Any,
) -> ToolResult:
    payload = _dump_sdk_value(response)
    if not isinstance(payload, dict):
        payload = {}
    content = payload.get("content") or []
    structured = (
        payload.get("structuredContent")
        or payload.get("structured_content")
        or payload.get("structuredcontent")
    )
    is_error = bool(payload.get("isError") or payload.get("is_error"))
    summary = _summary_from_sdk_payload(content=content, structured=structured)
    data = {
        "content": _sanitize_sdk_content(content),
        "structured_content": sanitize_error_detail(structured) if structured is not None else None,
        "is_error": is_error,
    }
    observation = _observation_from_sdk_payload(summary=summary, structured=structured)
    if is_error:
        return ToolResult(
            tool_name=namespaced_tool_name,
            success=False,
            error=sanitize_error_message(summary or "MCP tool returned an error."),
            data=data,
            model_observation=observation,
        )
    return ToolResult(
        tool_name=namespaced_tool_name,
        success=True,
        data=data,
        model_observation=observation,
        output_ref=f"mcp://{server.server_name}/{tool_name}",
    )


def _summary_from_sdk_payload(*, content: Any, structured: Any) -> str:
    if isinstance(structured, dict):
        summary = structured.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if item.get("type") == "text" and isinstance(text, str) and text.strip():
                return text
    return ""


def _observation_from_sdk_payload(*, summary: str, structured: Any) -> dict[str, Any]:
    if isinstance(structured, dict):
        sanitized = sanitize_error_detail(structured)
        if isinstance(sanitized, dict):
            return sanitized
    try:
        decoded = json.loads(summary)
    except (json.JSONDecodeError, TypeError):
        decoded = None
    if isinstance(decoded, dict):
        sanitized = sanitize_error_detail(decoded, _MCP_CONTENT_POLICY)
        if isinstance(sanitized, dict):
            return sanitized
    return {"summary": sanitize_error_message(summary or "MCP tool completed.")}


def _sanitize_sdk_content(content: Any) -> Any:
    if not isinstance(content, list):
        return sanitize_error_detail(content, _MCP_CONTENT_POLICY)
    normalized: list[Any] = []
    for item in content:
        if not isinstance(item, dict):
            normalized.append(sanitize_error_detail(item, _MCP_CONTENT_POLICY))
            continue
        copied = sanitize_error_detail(
            {key: value for key, value in item.items() if key != "text"},
            _MCP_CONTENT_POLICY,
        )
        if not isinstance(copied, dict):
            continue
        raw_text = item.get("text")
        if item.get("type") == "text" and isinstance(raw_text, str):
            copied["text"] = _sanitize_text_content(raw_text)
        normalized.append(copied)
    return normalized


def _sanitize_text_content(text: str) -> str:
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return sanitize_error_message(text, _MCP_CONTENT_POLICY)
    return json.dumps(
        sanitize_error_detail(decoded, _MCP_CONTENT_POLICY),
        ensure_ascii=False,
        sort_keys=True,
    )


def _dump_sdk_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_dump_sdk_value(item) for item in value]
    if isinstance(value, tuple):
        return [_dump_sdk_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _dump_sdk_value(child) for key, child in value.items()}
    return value
