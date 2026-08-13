"""Ownership boundary for a composed Assistant runtime and its resources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_persistence import close_trace_store
from assistant_agent.runtime.checkpointer import AsyncCheckpointerOwner


class RuntimeHost:
    """Own one runtime and the trace store assembled for its entry process."""

    def __init__(
        self,
        *,
        runtime: Any,
        owned_trace_store: Any | None = None,
        owned_checkpointer_owner: AsyncCheckpointerOwner | None = None,
    ) -> None:
        self._runtime = runtime
        self.owned_trace_store = owned_trace_store
        self.owned_checkpointer_owner = owned_checkpointer_owner
        self._close_lock = Lock()
        self._async_close_lock = asyncio.Lock()
        self._async_condition = asyncio.Condition()
        self._async_invocations = 0
        self._async_closing = False
        self._closed = False
        self._close_result = True

    @classmethod
    async def aopen(
        cls,
        *,
        config: ProviderConfig,
        runtime_factory: Callable[..., Any],
        owned_trace_store: Any | None = None,
    ) -> RuntimeHost:
        """Open process-owned graph resources before composing the Runtime."""

        owner = AsyncCheckpointerOwner(config)
        await owner.open()
        try:
            runtime = runtime_factory(
                checkpointer=owner.checkpointer,
                graph_invocation_claim_store=owner.invocation_claim_store,
            )
        except BaseException:
            await owner.aclose()
            raise
        return cls(
            runtime=runtime,
            owned_trace_store=owned_trace_store,
            owned_checkpointer_owner=owner,
        )

    @property
    def trace_store(self) -> Any:
        return self._runtime.trace_store

    @property
    def runtime(self) -> Any:
        """Expose raw Runtime only for legacy synchronous, nonpersistent hosts."""

        if self.owned_checkpointer_owner is not None:
            raise RuntimeError(
                "Async-owned Runtime is private; use RuntimeHost async methods."
            )
        return self._runtime

    def run_state(self, request: Any, **kwargs: Any) -> Any:
        if self.owned_checkpointer_owner is not None:
            raise RuntimeError(
                "Async-owned Runtime does not expose synchronous graph execution."
            )
        if self._closed:
            raise RuntimeError("runtime host is closed")
        return self._runtime.run_state(request, **kwargs)

    async def arun_state(self, request: Any, **kwargs: Any) -> Any:
        """Run one async invocation while preventing saver shutdown races."""

        await self._begin_async_invocation()
        try:
            return await self._runtime.arun_state(request, **kwargs)
        finally:
            await self._finish_async_invocation()

    async def aresume_state(self, request: Any, **kwargs: Any) -> Any:
        """Resume through the process host's async resource lease."""

        await self._begin_async_invocation()
        try:
            return await self._runtime.aresume_state(request, **kwargs)
        finally:
            await self._finish_async_invocation()

    async def areplay_state(self, owner: Any, request: Any, **kwargs: Any) -> Any:
        """Replay through the process host's async resource lease."""

        await self._begin_async_invocation()
        try:
            return await self._runtime.areplay_state(owner, request, **kwargs)
        finally:
            await self._finish_async_invocation()

    async def afork_state(self, owner: Any, request: Any, **kwargs: Any) -> Any:
        """Fork through the process host's async resource lease."""

        await self._begin_async_invocation()
        try:
            return await self._runtime.afork_state(owner, request, **kwargs)
        finally:
            await self._finish_async_invocation()

    async def alist_history(self, owner: Any, **kwargs: Any) -> Any:
        """Read history while the process host retains saver ownership."""

        await self._begin_async_invocation()
        try:
            return await self._runtime.alist_history(owner, **kwargs)
        finally:
            await self._finish_async_invocation()

    async def adelete_assistant_thread(self, **kwargs: Any) -> Any:
        """Delete one thread while saver and claim resources remain leased."""

        await self._begin_async_invocation()
        try:
            return await self._runtime.adelete_assistant_thread(**kwargs)
        finally:
            await self._finish_async_invocation()

    async def astream_state(self, request: Any, **kwargs: Any) -> Any:
        """Start a state stream and retain its lease through terminal result."""

        await self._begin_async_invocation()
        try:
            stream = self._runtime.astream_state(request, **kwargs)
        except BaseException:
            await self._finish_async_invocation()
            raise

        async def release_after_result() -> None:
            try:
                await stream.result()
            except BaseException:
                pass
            finally:
                await self._finish_async_invocation()

        asyncio.create_task(release_after_result())
        return stream

    def close(self, *, timeout: float = 1.0) -> bool:
        """Close Runtime services, then flush and close the owned trace store once."""

        if self.owned_checkpointer_owner is not None:
            raise RuntimeError(
                "RuntimeHost owns async graph resources; use await host.aclose()."
            )
        with self._close_lock:
            if self._closed:
                return self._close_result
            self._closed = True
            deadline = monotonic() + max(0.0, timeout)
            runtime_closed = _close_runtime(self._runtime)
            trace_closed = close_trace_store(
                self.owned_trace_store,
                timeout=max(0.0, deadline - monotonic()),
            )
            self._close_result = runtime_closed and trace_closed
            return self._close_result

    async def aclose(self, *, timeout: float = 1.0) -> bool:
        """Close Runtime and trace consumers before async graph resources."""

        async with self._async_close_lock:
            async with self._async_condition:
                if self._closed:
                    return self._close_result
                self._async_closing = True
                try:
                    async with asyncio.timeout(max(0.0, timeout)):
                        while self._async_invocations:
                            await self._async_condition.wait()
                except TimeoutError:
                    self._close_result = False
                    return False
                self._closed = True
            deadline = monotonic() + max(0.0, timeout)
            runtime_closed = _close_runtime(self._runtime)
            trace_closed = close_trace_store(
                self.owned_trace_store,
                timeout=max(0.0, deadline - monotonic()),
            )
            graph_closed = True
            if self.owned_checkpointer_owner is not None:
                try:
                    await self.owned_checkpointer_owner.aclose()
                except Exception:
                    graph_closed = False
            self._close_result = runtime_closed and trace_closed and graph_closed
            return self._close_result

    async def _begin_async_invocation(self) -> None:
        async with self._async_condition:
            if self._closed or self._async_closing:
                raise RuntimeError("runtime host is closing or closed")
            self._async_invocations += 1

    async def _finish_async_invocation(self) -> None:
        async with self._async_condition:
            self._async_invocations -= 1
            if not self._async_invocations:
                self._async_condition.notify_all()

    async def __aenter__(self) -> RuntimeHost:
        if self._closed:
            raise RuntimeError("runtime host is closed")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()


def _close_runtime(runtime: Any) -> bool:
    close = getattr(runtime, "close", None)
    if not callable(close):
        return True
    try:
        result = close()
    except Exception:
        return False
    return result is not False
