from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway.queueing import (
    GatewayQueuePolicy,
    GatewayRunAdmissionController,
    QueueOverflowError,
)


class GatewayQueuePolicyTests(unittest.TestCase):
    def test_defaults_are_bounded(self) -> None:
        policy = GatewayQueuePolicy()

        assert policy.mode == "followup"
        assert policy.max_pending_per_session == 8
        assert policy.max_queued_turns_global == 64
        assert policy.max_active_runs == 4
        assert policy.queue_wait_timeout_ms == 120_000
        assert policy.dedupe_ttl_s == 300.0
        assert policy.dedupe_max_entries_per_user == 1024
        assert policy.overflow_policy == "reject_newest"

    def test_non_positive_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_active_runs must be positive"):
            GatewayQueuePolicy(max_active_runs=0)

    def test_unknown_mode_and_overflow_policy_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode must be followup or interrupt"):
            GatewayQueuePolicy(mode="collect")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "overflow_policy must be reject_newest"):
            GatewayQueuePolicy(overflow_policy="drop_oldest")  # type: ignore[arg-type]


class GatewayRunAdmissionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_waiters_respect_active_cap(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=4)
        )
        first = await controller.reserve(
            user_id="u1",
            session_id="s1",
            turn_id="t1",
            run_id="r1",
        )
        second = await controller.reserve(
            user_id="u2",
            session_id="s2",
            turn_id="t2",
            run_id="r2",
        )

        first_ticket = await controller.request_permit(first)
        second_ticket = await controller.request_permit(second)

        first_permit = await asyncio.wait_for(first_ticket.ready, timeout=0.2)
        assert second_ticket.ready.done() is False
        assert (await controller.snapshot()).active_runs == 1

        await controller.release_permit(first_permit)

        second_permit = await asyncio.wait_for(second_ticket.ready, timeout=0.2)
        assert second_permit.run_id == "r2"
        await controller.release_permit(second_permit)
        assert (await controller.snapshot()).active_runs == 0

    async def test_global_queue_overflow_rejects_newest(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=1)
        )
        await controller.reserve(
            user_id="u1",
            session_id="s1",
            turn_id="t1",
            run_id="r1",
        )

        with self.assertRaises(QueueOverflowError) as raised:
            await controller.reserve(
                user_id="u2",
                session_id="s2",
                turn_id="t2",
                run_id="r2",
            )

        assert raised.exception.scope == "global"
        assert (await controller.snapshot()).queued_turns == 1

    async def test_cancel_waiting_ticket_releases_reservation(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=3)
        )
        first = await controller.reserve(
            user_id="u1",
            session_id="s1",
            turn_id="t1",
            run_id="r1",
        )
        second = await controller.reserve(
            user_id="u2",
            session_id="s2",
            turn_id="t2",
            run_id="r2",
        )
        first_ticket = await controller.request_permit(first)
        second_ticket = await controller.request_permit(second)
        first_permit = await first_ticket.ready

        assert await controller.cancel_ticket(second_ticket) is True
        assert await controller.cancel_ticket(second_ticket) is False
        assert (await controller.snapshot()).queued_turns == 0
        await controller.release_permit(first_permit)

    async def test_release_permit_is_idempotent(self) -> None:
        controller = GatewayRunAdmissionController(GatewayQueuePolicy(max_active_runs=1))
        reservation = await controller.reserve(
            user_id="u1",
            session_id="s1",
            turn_id="t1",
            run_id="r1",
        )
        ticket = await controller.request_permit(reservation)
        permit = await ticket.ready

        assert await controller.release_permit(permit) is True
        assert await controller.release_permit(permit) is False
        assert await controller.release_reservation(reservation) is False

    async def test_cancelled_ready_future_does_not_leak_a_permit(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=3)
        )
        first = await controller.reserve(
            user_id="u1",
            session_id="s1",
            turn_id="t1",
            run_id="r1",
        )
        second = await controller.reserve(
            user_id="u2",
            session_id="s2",
            turn_id="t2",
            run_id="r2",
        )
        first_ticket = await controller.request_permit(first)
        second_ticket = await controller.request_permit(second)
        first_permit = await first_ticket.ready
        second_ticket.ready.cancel()

        await controller.release_permit(first_permit)

        snapshot = await controller.snapshot()
        assert snapshot.active_runs == 0
        assert snapshot.queued_turns == 0
        assert snapshot.waiting_turns == 0

    async def test_close_cancels_waiters_and_rejects_new_reservations(self) -> None:
        controller = GatewayRunAdmissionController(
            GatewayQueuePolicy(max_active_runs=1, max_queued_turns_global=3)
        )
        first = await controller.reserve(
            user_id="u1",
            session_id="s1",
            turn_id="t1",
            run_id="r1",
        )
        second = await controller.reserve(
            user_id="u2",
            session_id="s2",
            turn_id="t2",
            run_id="r2",
        )
        await controller.reserve(
            user_id="u3",
            session_id="s3",
            turn_id="t3",
            run_id="r3",
        )
        first_permit = await (await controller.request_permit(first)).ready
        second_ticket = await controller.request_permit(second)

        await controller.close()

        assert second_ticket.ready.cancelled() is True
        assert (await controller.snapshot()).waiting_turns == 0
        assert (await controller.snapshot()).queued_turns == 0
        with self.assertRaisesRegex(RuntimeError, "admission controller is closed"):
            await controller.reserve(
                user_id="u3",
                session_id="s3",
                turn_id="t3",
                run_id="r3",
            )
        await controller.release_permit(first_permit)
