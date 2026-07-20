"""Minimal stdio MCP client runner for explicitly configured external tools."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from collections.abc import Mapping
from typing import Any, BinaryIO

from assistant_agent.mcp.adapter import (
    MCPToolDefinition,
    MCPToolRunner,
    namespaced_mcp_tool_name,
)
from assistant_agent.mcp.config import MCPServerConfig, MCPToolAdapterConfig
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


class MCPClientError(RuntimeError):
    """Raised when an external MCP server cannot be queried safely."""


class StdioMCPClientRunner(MCPToolRunner):
    """Discover and execute tools from configured stdio MCP servers."""

    def __init__(self, servers: list[MCPServerConfig]) -> None:
        self._servers = {server.server_name: server for server in servers}

    def list_tools(self, *, server: MCPServerConfig) -> list[MCPToolDefinition]:
        response = self._request(server, "tools/list", {})
        result = _response_result(response)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPClientError("MCP tools/list response did not include tools.")
        definitions: list[MCPToolDefinition] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            input_schema = item.get("inputSchema") or item.get("input_schema") or {}
            if not isinstance(input_schema, dict):
                input_schema = {}
            definitions.append(
                MCPToolDefinition(
                    name=name,
                    description=str(item.get("description") or ""),
                    input_schema=input_schema,
                )
            )
        return definitions

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
            response = self._request(
                server,
                "tools/call",
                {"name": tool_name, "arguments": tool_input},
            )
            return _tool_result_from_response(
                server=server,
                tool_name=tool_name,
                namespaced_tool_name=namespaced_tool_name,
                response=response,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=namespaced_tool_name,
                success=False,
                error=sanitize_error_message(exc),
            )

    def _request(
        self,
        server: MCPServerConfig,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        process = _start_process(server)
        try:
            _send_message(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "assistant_agent",
                            "version": "local",
                        },
                    },
                },
            )
            initialize_response = _read_response(
                process.stdout,
                expected_id=1,
                timeout_seconds=server.timeout_seconds,
            )
            _raise_for_error(initialize_response)
            _send_message(
                process.stdin,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            _send_message(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": method,
                    "params": params,
                },
            )
            response = _read_response(
                process.stdout,
                expected_id=2,
                timeout_seconds=server.timeout_seconds,
            )
            _raise_for_error(response)
            return response
        finally:
            _close_process(process)


def _start_process(server: MCPServerConfig) -> subprocess.Popen[bytes]:
    env = _process_env(server.env)
    try:
        return subprocess.Popen(
            server.command,
            cwd=server.cwd,
            env=env,
            bufsize=0,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # pragma: no cover - OS/process boundary
        message = sanitize_error_message(exc)
        raise MCPClientError(f"Failed to start MCP server {server.server_name}: {message}") from exc


def _process_env(server_env: Mapping[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in server_env.items()})
    return env


def _send_message(stream: BinaryIO | None, payload: dict[str, Any]) -> None:
    if stream is None:
        raise MCPClientError("MCP server stdin is unavailable.")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


def _read_response(
    stream: BinaryIO | None,
    *,
    expected_id: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if stream is None:
        raise MCPClientError("MCP server stdout is unavailable.")
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP server response timed out.")
        message = _read_message(stream, timeout_seconds=remaining)
        if message.get("id") == expected_id:
            return message


def _read_message(stream: BinaryIO, *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    headers: dict[str, str] = {}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP server response timed out.")
        ready, _, _ = select.select([stream], [], [], remaining)
        if not ready:
            raise TimeoutError("MCP server response timed out.")
        line = stream.readline()
        if not line:
            raise MCPClientError("MCP server closed stdout.")
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        if key:
            headers[key.lower()] = value.strip()
    length_text = headers.get("content-length")
    if length_text is None:
        raise MCPClientError("MCP response missing Content-Length header.")
    try:
        length = int(length_text)
    except ValueError as exc:
        raise MCPClientError("MCP response has invalid Content-Length header.") from exc
    body = _read_exact(stream, length=length, deadline=deadline)
    try:
        message = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise MCPClientError("MCP response body is not valid JSON.") from exc
    if not isinstance(message, dict):
        raise MCPClientError("MCP response body is not a JSON object.")
    return message


def _read_exact(stream: BinaryIO, *, length: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining_bytes = length
    while remaining_bytes > 0:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise TimeoutError("MCP server response timed out.")
        ready, _, _ = select.select([stream], [], [], remaining_time)
        if not ready:
            raise TimeoutError("MCP server response timed out.")
        chunk = stream.read(remaining_bytes)
        if not chunk:
            raise MCPClientError("MCP server closed stdout.")
        chunks.append(chunk)
        remaining_bytes -= len(chunk)
    return b"".join(chunks)


def _raise_for_error(response: dict[str, Any]) -> None:
    error = response.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message") or error
    raise MCPClientError(sanitize_error_message(message))


def _response_result(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        raise MCPClientError("MCP response did not include a result object.")
    return result


def _tool_result_from_response(
    *,
    server: MCPServerConfig,
    tool_name: str,
    namespaced_tool_name: str,
    response: dict[str, Any],
) -> ToolResult:
    result = _response_result(response)
    content = result.get("content") or []
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    is_error = bool(result.get("isError") or result.get("is_error"))
    summary = _summary_from_tool_result(content=content, structured=structured)
    data = {
        "content": sanitize_error_detail(content),
        "structured_content": sanitize_error_detail(structured) if structured is not None else None,
        "is_error": is_error,
    }
    observation = _observation_from_tool_result(summary=summary, structured=structured)
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


def _summary_from_tool_result(*, content: Any, structured: Any) -> str:
    if isinstance(structured, dict):
        summary = structured.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return ""


def _observation_from_tool_result(*, summary: str, structured: Any) -> dict[str, Any]:
    if isinstance(structured, dict):
        sanitized = sanitize_error_detail(structured)
        if isinstance(sanitized, dict):
            return sanitized
    return {"summary": sanitize_error_message(summary or "MCP tool completed.")}


def _close_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except Exception:
        pass
    try:
        process.terminate()
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
    except Exception:
        pass
