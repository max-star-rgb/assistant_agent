"""Public Agent Server SDK boundary used by custom routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol

from langgraph_sdk import get_client


class AgentServerClient(Protocol):
    async def create_thread(self, *, metadata: Mapping[str, object]) -> str: ...

    def stream_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: Mapping[str, object],
        context: Mapping[str, object],
        multitask_strategy: str,
        on_run_created: Callable[[str], None],
    ) -> AsyncIterator[Mapping[str, Any]]: ...

    async def cancel_run(self, *, thread_id: str, run_id: str) -> None: ...

    def join_thread(
        self, *, thread_id: str, last_event_id: str | None
    ) -> AsyncIterator[Mapping[str, Any]]: ...


class SdkAgentServerClient:
    """Thin loopback SDK adapter; it owns no run or queue state."""

    def __init__(
        self,
        *,
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._client = get_client(url=url, headers=headers)

    async def create_thread(self, *, metadata: Mapping[str, object]) -> str:
        thread = await self._client.threads.create(metadata=dict(metadata))
        return str(thread["thread_id"])

    async def stream_run(self, **kwargs):
        callback = kwargs.pop("on_run_created")

        def created(metadata: Mapping[str, Any]) -> None:
            callback(str(metadata["run_id"]))

        async for part in self._client.runs.stream(
            kwargs.pop("thread_id"),
            kwargs.pop("assistant_id"),
            stream_mode=["values"],
            stream_resumable=True,
            on_run_created=created,
            **kwargs,
        ):
            yield {"event": part.event, "data": part.data, "id": part.id}

    async def cancel_run(self, *, thread_id: str, run_id: str) -> None:
        await self._client.runs.cancel(thread_id, run_id, wait=True)

    async def join_thread(self, *, thread_id: str, last_event_id: str | None):
        async for part in self._client.threads.join_stream(
            thread_id,
            last_event_id=last_event_id,
            stream_mode="run_modes",
        ):
            yield {"event": part.event, "data": part.data, "id": part.id}


__all__ = ["AgentServerClient", "SdkAgentServerClient"]
