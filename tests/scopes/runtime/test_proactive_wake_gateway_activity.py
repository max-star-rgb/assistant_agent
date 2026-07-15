from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway import GatewaySessionManager, GatewaySessionService, frame
from assistant_agent.realtime import RealtimeAgentResult
from assistant_agent.schemas.proactive_wake import WakeOwner
from assistant_agent.services.proactive_wake.activity import (
    GatewayUserActivityReader,
    NullUserActivityReader,
)


class BlockingRealtimeBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.started.set()
        await self.release.wait()
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            expects_reply=True,
        )


async def _collect_until_run_end(endpoint):
    frames = []
    async for received in endpoint:
        frames.append(received)
        if received["type"] == "run.end":
            return frames
    raise AssertionError("endpoint closed before run.end")


class GatewayActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_activity_reader_tracks_active_run_not_idle_session(self) -> None:
        backend = BlockingRealtimeBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        reader = GatewayUserActivityReader(manager)
        owner = WakeOwner(user_id="u1")
        handle = await manager.acquire(user_id=owner.user_id)

        try:
            self.assertFalse(await reader.is_active(owner))

            await handle.endpoint.send(
                frame(
                    type="message.user",
                    user_id=owner.user_id,
                    session_id="activity-session",
                    payload={"text": "block this run"},
                )
            )
            await asyncio.wait_for(backend.started.wait(), timeout=1.0)

            self.assertTrue(await reader.is_active(owner))

            backend.release.set()
            frames = await asyncio.wait_for(
                _collect_until_run_end(handle.endpoint),
                timeout=1.0,
            )

            self.assertEqual(frames[-1]["type"], "run.end")
            self.assertFalse(await reader.is_active(owner))
            self.assertTrue(manager.has_active_session(owner.user_id))
        finally:
            backend.release.set()
            await manager.close()

    async def test_gateway_activity_reader_returns_false_for_unknown_user(self) -> None:
        lifecycle_events = []
        manager = GatewaySessionManager(
            start_reaper=False,
            lifecycle_sink=lifecycle_events.append,
        )
        reader = GatewayUserActivityReader(manager)
        owner = WakeOwner(user_id="unknown-user")

        try:
            self.assertFalse(await reader.is_active(owner))
            self.assertEqual(manager.active_count(), 0)
            self.assertFalse(manager.has_active_session(owner.user_id))
            self.assertIsNone(manager.session_config(owner.user_id))
            self.assertEqual(lifecycle_events, [])
        finally:
            await manager.close()

    async def test_manager_releases_its_lock_before_awaiting_service_snapshot(self) -> None:
        class BlockingSnapshotService(GatewaySessionService):
            def __init__(self) -> None:
                super().__init__()
                self.snapshot_started = asyncio.Event()
                self.snapshot_release = asyncio.Event()

            async def has_active_run(self) -> bool:
                self.snapshot_started.set()
                await self.snapshot_release.wait()
                return False

        service = BlockingSnapshotService()
        manager = GatewaySessionManager(
            service_factory=lambda user_id, config: service,
            start_reaper=False,
        )
        await manager.acquire(user_id="u1")
        snapshot_task = asyncio.create_task(manager.has_active_run("u1"))

        try:
            await asyncio.wait_for(service.snapshot_started.wait(), timeout=1.0)

            self.assertTrue(
                await asyncio.wait_for(manager.destroy("u1"), timeout=1.0)
            )
            service.snapshot_release.set()
            self.assertFalse(await asyncio.wait_for(snapshot_task, timeout=1.0))
        finally:
            service.snapshot_release.set()
            await asyncio.gather(snapshot_task, return_exceptions=True)
            await manager.close()

    async def test_null_activity_reader_is_always_inactive(self) -> None:
        reader = NullUserActivityReader()

        self.assertFalse(await reader.is_active(WakeOwner(user_id="u1")))


if __name__ == "__main__":
    unittest.main()
