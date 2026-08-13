from __future__ import annotations

import asyncio
from types import SimpleNamespace

from assistant_agent.agent_server.client import SdkAgentServerClient


class _RunClient:
    async def stream(self, *_args, **kwargs):
        kwargs["on_run_created"]({"run_id": "run-1"})
        yield SimpleNamespace(event="metadata", data={"run_id": "run-1"}, id="event-1")
        raise ConnectionError("transient stream disconnect")


class _ThreadClient:
    def __init__(self) -> None:
        self.last_event_id = None

    async def join_stream(self, _thread_id, *, last_event_id, stream_mode):
        self.last_event_id = last_event_id

        async def events():
            yield SimpleNamespace(event="values", data={"answer": "done"}, id="event-2")
            yield SimpleNamespace(
                event="metadata",
                data={"status": "run_done", "run_id": "run-1"},
                id="event-3",
            )

        return events()


def test_sdk_client_rejoins_resumable_thread_stream_after_transport_disconnect() -> None:
    client = object.__new__(SdkAgentServerClient)
    threads = _ThreadClient()
    client._client = SimpleNamespace(runs=_RunClient(), threads=threads)
    created = []

    async def exercise():
        return [
            part
            async for part in client.stream_run(
                thread_id="thread-1",
                assistant_id="assistant",
                input={},
                context={},
                multitask_strategy="enqueue",
                on_run_created=created.append,
            )
        ]

    parts = asyncio.run(exercise())

    assert created == ["run-1"]
    assert threads.last_event_id == "event-1"
    assert [part["id"] for part in parts] == ["event-1", "event-2", "event-3"]
