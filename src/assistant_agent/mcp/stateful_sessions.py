"""Thread-scoped MCP sessions backed by the official client adapter."""

from __future__ import annotations

import asyncio
import time
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
    AssistantRunContext,
    authenticated_user_identity,
)


MCPClientFactory = Callable[..., Any]


def resolve_mcp_connection(
    server: MCPServerConfig,
    *,
    cwd: Path | None = None,
    discovery_root: Path | None = None,
) -> dict[str, Any]:
    """Build one official stdio connection with server-owned path expansion."""

    if cwd is not None:
        replacements = {"{cwd}": str(cwd)}
    elif discovery_root is not None:
        repository = discovery_root / "repo"
        repository.mkdir(parents=True, exist_ok=True)
        replacements = {"{cwd}": str(repository)}
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
    session: ClientSession
    stack: AsyncExitStack
    call_lock: asyncio.Lock
    last_used_at: float


class ThreadMcpSessionPool:
    """Own one initialized MCP ClientSession per trusted thread/server scope."""

    def __init__(
        self,
        server_configs: Sequence[MCPServerConfig],
        *,
        client_factory: MCPClientFactory = MultiServerMCPClient,
    ) -> None:
        self._configs = {config.server_name: config for config in server_configs}
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
        context = getattr(runtime, "context", None)
        if not isinstance(context, AssistantRunContext):
            context = AssistantRunContext(cwd=getattr(context, "cwd", None))
        key = (identity, thread_id, request.server_name)
        entry = await self._entry(key, config, context.cwd)
        async with entry.call_lock:
            entry.last_used_at = time.monotonic()
            try:
                return await entry.session.call_tool(request.name, request.args)
            finally:
                entry.last_used_at = time.monotonic()

    async def _entry(
        self,
        key: tuple[str, str, str],
        config: MCPServerConfig,
        cwd: Path,
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
            connection = resolve_mcp_connection(config, cwd=cwd)
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
                session=session,
                stack=stack,
                call_lock=asyncio.Lock(),
                last_used_at=time.monotonic(),
            )
            async with self._lock:
                self._entries[key] = entry
                self._entry_locks.pop(key, None)
            return entry

    async def aclose_idle(self, max_idle_seconds: int) -> None:
        cutoff = time.monotonic() - max_idle_seconds
        async with self._lock:
            matches = [
                (key, entry)
                for key, entry in self._entries.items()
                if entry.last_used_at <= cutoff and not entry.call_lock.locked()
            ]
            for key, _entry in matches:
                self._entries.pop(key, None)
                self._entry_locks.pop(key, None)
        await _close_entries([entry for _key, entry in matches])

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
