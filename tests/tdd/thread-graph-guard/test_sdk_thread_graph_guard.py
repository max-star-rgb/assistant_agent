from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_agent.agent_server.client import SdkAgentServerClient
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID, MEMORY_GRAPH_ID


class _FakeThreads:
    def __init__(self, thread: dict[str, Any], *, echo_create: bool = False) -> None:
        self.thread = thread
        self.echo_create = echo_create
        self.create_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        if self.echo_create:
            return {
                "thread_id": kwargs.get("thread_id") or "thread-created",
                "metadata": kwargs["metadata"],
            }
        return self.thread

    async def get(self, thread_id: str) -> dict[str, Any]:
        self.get_calls.append(thread_id)
        return self.thread


class _FakeRuns:
    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, *_args: Any, **_kwargs: Any):
        self.stream_calls += 1
        yield SimpleNamespace(event="values", data={"messages": []}, id="event-1")


class _FakeSdk:
    def __init__(self, thread: dict[str, Any], *, echo_create: bool = False) -> None:
        self.threads = _FakeThreads(thread, echo_create=echo_create)
        self.runs = _FakeRuns()


def _client_for(
    thread: dict[str, Any],
    *,
    echo_create: bool = False,
) -> tuple[SdkAgentServerClient, _FakeSdk]:
    sdk = _FakeSdk(thread, echo_create=echo_create)
    client = object.__new__(SdkAgentServerClient)
    client._client = sdk
    return client, sdk


async def _collect_run(
    client: SdkAgentServerClient,
    *,
    assistant_id: str = ASSISTANT_GRAPH_ID,
) -> list[dict[str, Any]]:
    return [
        part
        async for part in client.stream_run(
            thread_id="thread-sentinel",
            assistant_id=assistant_id,
            input={"messages": [{"role": "user", "content": "hello"}]},
            context={"entry_profile": "agent_service"},
            multitask_strategy="enqueue",
            on_run_created=lambda _run_id: None,
        )
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        {"assistant_graph_id": "assistant-native-v1"},
        {},
    ],
    ids=("v1", "missing"),
)
def test_stream_rejects_thread_not_bound_to_expected_graph_before_run_start(
    metadata: dict[str, Any],
) -> None:
    """Catches a normal v2 run starting on a v1 or unknown thread."""

    client, sdk = _client_for(
        {"thread_id": "thread-sentinel", "metadata": metadata}
    )

    with pytest.raises(ValueError, match="thread graph"):
        asyncio.run(_collect_run(client))

    assert sdk.runs.stream_calls == 0


def test_stream_allows_thread_bound_to_expected_graph() -> None:
    """Catches the guard rejecting a correctly bound runnable thread."""

    client, sdk = _client_for(
        {
            "thread_id": "thread-sentinel",
            "metadata": {"assistant_graph_id": ASSISTANT_GRAPH_ID},
        }
    )

    assert asyncio.run(_collect_run(client)) == [
        {"event": "values", "data": {"messages": []}, "id": "event-1"}
    ]
    assert sdk.threads.get_calls == ["thread-sentinel"]
    assert sdk.runs.stream_calls == 1


def test_create_thread_writes_stable_graph_identity_metadata() -> None:
    """Catches a newly runnable thread being created without a graph binding."""

    client, sdk = _client_for({}, echo_create=True)

    thread_id = asyncio.run(
        client.create_thread(
            metadata={"protocol": "agent-service-v1"},
            graph_id=ASSISTANT_GRAPH_ID,
        )
    )

    assert thread_id == "thread-created"
    assert sdk.threads.create_calls[0]["metadata"] == {
        "protocol": "agent-service-v1",
        "assistant_graph_id": ASSISTANT_GRAPH_ID,
    }
    assert sdk.threads.create_calls[0]["graph_id"] == ASSISTANT_GRAPH_ID


def test_create_thread_do_nothing_rejects_returned_existing_graph_mismatch() -> None:
    """Catches create intent metadata masking an existing legacy thread."""

    client, sdk = _client_for(
        {
            "thread_id": "thread-existing",
            "metadata": {"assistant_graph_id": "assistant-native-v1"},
        }
    )

    with pytest.raises(ValueError, match="thread graph"):
        asyncio.run(
            client.create_thread(
                metadata={"protocol": "agent-service-v1"},
                thread_id="thread-existing",
                graph_id=ASSISTANT_GRAPH_ID,
            )
        )

    assert sdk.threads.create_calls[0]["if_exists"] == "do_nothing"


def test_guard_uses_the_callers_expected_graph_for_independent_threads() -> None:
    """Catches the assistant v2 identity being hardcoded over another graph."""

    creator, create_sdk = _client_for({}, echo_create=True)
    assert asyncio.run(
        creator.create_thread(metadata={"kind": "memory"}, graph_id=MEMORY_GRAPH_ID)
    ) == "thread-created"
    assert create_sdk.threads.create_calls[0]["metadata"] == {
        "kind": "memory",
        "assistant_graph_id": MEMORY_GRAPH_ID,
    }
    assert create_sdk.threads.create_calls[0]["graph_id"] == MEMORY_GRAPH_ID

    runner, run_sdk = _client_for(
        {
            "thread_id": "memory-thread",
            "metadata": {"assistant_graph_id": MEMORY_GRAPH_ID},
        }
    )
    assert asyncio.run(_collect_run(runner, assistant_id=MEMORY_GRAPH_ID))
    assert run_sdk.runs.stream_calls == 1
