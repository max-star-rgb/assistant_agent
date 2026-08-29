"""Thread-scoped MCP sessions backed by the official client adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import (
    MCPToolCallRequest,
    MCPToolCallResult,
)
from mcp import ClientSession

from assistant_agent.mcp.config import (
    MCPServerConfig,
    resolve_mcp_server_env,
)
from assistant_agent.native_agent.context import (
    authenticated_user_identity,
)
from assistant_agent.runtime.thread_resources import (
    ThreadResourceManager,
    ThreadResources,
)


MCPClientFactory = Callable[..., Any]


def resolve_mcp_connection(
    server: MCPServerConfig,
    *,
    resources: ThreadResources | Any | None = None,
    discovery_root: Path | None = None,
) -> dict[str, Any]:
    """Build one official stdio connection with server-owned path expansion."""

    if resources is not None:
        replacements = {
            "{workspace_root}": str(resources.scratch_root),
            "{repo_root}": str(resources.scratch_root),
            "{artifact_root}": str(resources.artifact_root),
        }
    elif discovery_root is not None:
        repository = discovery_root / "repo"
        artifacts = discovery_root / "artifacts"
        repository.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        replacements = {
            "{workspace_root}": str(repository),
            "{repo_root}": str(repository),
            "{artifact_root}": str(artifacts),
        }
    else:
        replacements = {}

    def expand(value: str) -> str:
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value

    command, *args = (expand(value) for value in server.command)
    connection: dict[str, Any] = {
        "transport": "stdio",
        "command": command,
        "args": args,
        "env": resolve_mcp_server_env(server.env),
    }
    if server.cwd is not None:
        connection["cwd"] = expand(server.cwd)
    return connection


@dataclass
class _SessionEntry:
    thread_ref: str
    session: ClientSession
    stack: AsyncExitStack
    call_lock: asyncio.Lock


class ThreadMcpSessionPool:
    """Own one initialized MCP ClientSession per trusted thread/server scope."""

    def __init__(
        self,
        server_configs: Sequence[MCPServerConfig],
        *,
        manager: ThreadResourceManager,
        client_factory: MCPClientFactory = MultiServerMCPClient,
    ) -> None:
        self._configs = {config.server_name: config for config in server_configs}
        self._manager = manager
        self._client_factory = client_factory
        self._entries: dict[tuple[str, str, str], _SessionEntry] = {}
        self._entry_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def call(self, request: MCPToolCallRequest) -> MCPToolCallResult:
        config = self._configs.get(request.server_name)
        if config is None or config.session_scope != "thread":
            raise ValueError("stateful MCP server is not configured")
        runtime = request.runtime
        if runtime is None:
            raise ValueError("stateful MCP requires ToolRuntime")
        identity = authenticated_user_identity(runtime)
        execution_info = getattr(runtime, "execution_info", None)
        thread_id = str(getattr(execution_info, "thread_id", "") or "").strip()
        if not thread_id:
            raise ValueError("stateful MCP requires thread identity")
        resources = await asyncio.to_thread(self._manager.resolve, identity, thread_id)
        key = (identity, thread_id, request.server_name)
        entry = await self._entry(key, config, resources)
        async with entry.call_lock:
            return await entry.session.call_tool(request.name, request.args)

    async def _entry(
        self,
        key: tuple[str, str, str],
        config: MCPServerConfig,
        resources: ThreadResources,
    ) -> _SessionEntry:
        async with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing
            entry_lock = self._entry_locks.setdefault(key, asyncio.Lock())
        async with entry_lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing
            connection = resolve_mcp_connection(config, resources=resources)
            client = self._client_factory({config.server_name: connection})
            stack = AsyncExitStack()
            try:
                session = await stack.enter_async_context(
                    client.session(config.server_name)
                )
            except BaseException:
                await stack.aclose()
                raise
            entry = _SessionEntry(
                thread_ref=resources.thread_ref,
                session=session,
                stack=stack,
                call_lock=asyncio.Lock(),
            )
            async with self._lock:
                self._entries[key] = entry
                self._entry_locks.pop(key, None)
            return entry

    async def aclose_thread(self, thread_ref: str) -> None:
        async with self._lock:
            matches = [
                (key, entry)
                for key, entry in self._entries.items()
                if entry.thread_ref == thread_ref
            ]
            for key, _entry in matches:
                self._entries.pop(key, None)
                self._entry_locks.pop(key, None)
        await _close_entries([entry for _key, entry in matches])

    def active_thread_refs(self) -> frozenset[str]:
        return frozenset(entry.thread_ref for entry in self._entries.values())

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._entry_locks.clear()
        await _close_entries(entries)


class StatefulMcpInterceptor:
    """Route only explicitly thread-scoped MCP calls to the persistent pool."""

    def __init__(
        self,
        server_configs: Sequence[MCPServerConfig],
        pool: ThreadMcpSessionPool,
    ) -> None:
        self._configs = {config.server_name: config for config in server_configs}
        self._pool = pool

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
    ) -> MCPToolCallResult:
        config = self._configs.get(request.server_name)
        if config is None or config.session_scope == "call":
            return await handler(request)
        return await self._pool.call(request)


async def _close_entries(entries: list[_SessionEntry]) -> None:
    errors: list[BaseException] = []
    for entry in entries:
        try:
            await entry.stack.aclose()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("failed to close MCP sessions", errors)


__all__ = [
    "StatefulMcpInterceptor",
    "ThreadMcpSessionPool",
    "resolve_mcp_connection",
]
