"""Public Agent Server SDK boundary used by custom routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol

from httpx import TransportError
from langgraph_sdk import get_client


class AgentServerClient(Protocol):
    async def create_thread(
        self, *, metadata: Mapping[str, object], thread_id: str | None = None
    ) -> str: ...

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

    async def create_thread(
        self, *, metadata: Mapping[str, object], thread_id: str | None = None
    ) -> str:
        thread = await self._client.threads.create(
            metadata=dict(metadata),
            thread_id=thread_id,
            if_exists="do_nothing" if thread_id is not None else None,
        )
        return str(thread["thread_id"])

    async def stream_run(self, **kwargs):
        thread_id = kwargs.pop("thread_id")
        assistant_id = kwargs.pop("assistant_id")
        callback = kwargs.pop("on_run_created")
        run_id: str | None = None
        last_event_id: str | None = None

        def created(metadata: Mapping[str, Any]) -> None:
            nonlocal run_id
            run_id = str(metadata["run_id"])
            callback(run_id)

        try:
            async for part in self._client.runs.stream(
                thread_id,
                assistant_id,
                stream_mode=["messages", "updates", "values"],
                stream_resumable=True,
                on_disconnect="continue",
                on_run_created=created,
                **kwargs,
            ):
                if part.id is not None:
                    last_event_id = str(part.id)
                yield {"event": part.event, "data": part.data, "id": part.id}
        except (ConnectionError, OSError, TransportError):
            if run_id is None or last_event_id is None:
                raise
            stream = await self._client.threads.join_stream(
                thread_id,
                last_event_id=last_event_id,
                stream_mode="run_modes",
            )
            async for part in stream:
                yield {"event": part.event, "data": part.data, "id": part.id}
                if (
                    part.event == "metadata"
                    and isinstance(part.data, Mapping)
                    and part.data.get("status") == "run_done"
                    and str(part.data.get("run_id")) == run_id
                ):
                    break

    async def cancel_run(self, *, thread_id: str, run_id: str) -> None:
        await self._client.runs.cancel(thread_id, run_id, wait=True)

    async def join_thread(self, *, thread_id: str, last_event_id: str | None):
        stream = await self._client.threads.join_stream(
            thread_id,
            last_event_id=last_event_id,
            stream_mode="run_modes",
        )
        async for part in stream:
            yield {"event": part.event, "data": part.data, "id": part.id}


__all__ = ["AgentServerClient", "SdkAgentServerClient"]
