"""Async stream facade for synchronous agent runtime events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.event_sink import EventSink


TResult = TypeVar("TResult")
_MISSING = object()


@dataclass(frozen=True)
class _EventItem:
    event: AgentEvent


@dataclass(frozen=True)
class _DoneItem:
    pass


class AgentRunStream(Generic[TResult]):
    """Async iterator over runtime events plus an explicit final result."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[_EventItem | _DoneItem] = asyncio.Queue()
        self._result_future: asyncio.Future[TResult] = loop.create_future()
        self._finished = False

    def __aiter__(self) -> "AgentRunStream[TResult]":
        return self

    async def __anext__(self) -> AgentEvent:
        item = await self._queue.get()
        if isinstance(item, _EventItem):
            return item.event
        if self._result_future.done():
            self._result_future.result()
        raise StopAsyncIteration

    async def result(self) -> TResult:
        return await self._result_future

    async def wait(self) -> TResult:
        return await self.result()

    def emit(self, event: AgentEvent) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, _EventItem(event))

    def set_result(self, result: TResult) -> None:
        self._finish(result=result)

    def set_exception(self, exc: BaseException) -> None:
        self._finish(exc=exc)

    def _finish(
        self,
        *,
        result: TResult | object = _MISSING,
        exc: BaseException | None = None,
    ) -> None:
        def complete() -> None:
            if self._finished:
                return
            self._finished = True
            if exc is not None:
                self._result_future.set_exception(exc)
            else:
                self._result_future.set_result(result)  # type: ignore[arg-type]
            self._queue.put_nowait(_DoneItem())

        self._loop.call_soon_threadsafe(complete)


class AsyncQueueEventSink:
    """Thread-safe EventSink that forwards AgentEvent records to AgentRunStream."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        stream: AgentRunStream[Any],
        inner: EventSink | None = None,
    ) -> None:
        self._loop = loop
        self._stream = stream
        self._inner = inner

    def emit(self, event: AgentEvent) -> None:
        self._stream.emit(event)
        if self._inner is not None:
            self._inner.emit(event)
