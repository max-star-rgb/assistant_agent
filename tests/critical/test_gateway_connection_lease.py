"""Gateway connection ownership and session-owned relay contract."""

from __future__ import annotations

import asyncio

from assistant_agent.gateway.bridge import GatewayBridge
from assistant_agent.gateway.protocol import frame
from assistant_agent.gateway.transport import Endpoint, InMemoryDuplex


async def _receive(endpoint: Endpoint, *, timeout: float = 1.0) -> dict:
    return await asyncio.wait_for(anext(endpoint.__aiter__()), timeout=timeout)


def test_newest_connection_takes_over_session_relay_without_cancelling_run() -> None:
    asyncio.run(_assert_newest_connection_takes_over_session_relay())


async def _assert_newest_connection_takes_over_session_relay() -> None:
    runtime_bridge_ep, runtime_session_ep = InMemoryDuplex.create_pair()
    first_bridge_ep, first_client_ep = InMemoryDuplex.create_pair()
    second_bridge_ep, second_client_ep = InMemoryDuplex.create_pair()
    bridge = GatewayBridge()

    first_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-1",
            client_ep=first_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-1",
            session_id="session-1",
        )
    )
    await asyncio.sleep(0)

    await runtime_session_ep.send(
        frame(
            type="run.started",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
        )
    )
    assert (await _receive(first_client_ep))["type"] == "run.started"

    second_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-2",
            client_ep=second_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-1",
            session_id="session-1",
        )
    )
    await asyncio.wait_for(first_task, timeout=1.0)

    # Superseding the old connection is an ownership transfer, not a transport
    # disconnect, so it must not synthesize run.cancel.
    try:
        unexpected = await _receive(runtime_session_ep, timeout=0.05)
    except asyncio.TimeoutError:
        unexpected = None
    assert unexpected is None

    await runtime_session_ep.send(
        frame(
            type="stream.chunk",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
            payload={"text": "new owner only"},
        )
    )
    delivered = await _receive(second_client_ep)
    assert delivered["type"] == "stream.chunk"
    assert delivered["payload"]["text"] == "new owner only"

    try:
        stale_delivery = await _receive(first_client_ep, timeout=0.05)
    except asyncio.TimeoutError:
        stale_delivery = None
    assert stale_delivery is None

    # A true disconnect of the current owner cancels the run that was already
    # active before ownership changed.
    await second_client_ep.close()
    await asyncio.wait_for(second_task, timeout=1.0)
    cancel = await _receive(runtime_session_ep)
    assert cancel["type"] == "run.cancel"
    assert cancel["run_id"] == "run-1"
    assert cancel["payload"] == {
        "source": "gateway_disconnect",
        "reason": "client_disconnected",
    }

    await runtime_session_ep.close()
