"""Thread-scoped Media-Agent pull pump for proactive delivery rows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from assistant_agent.agent_server.media_protocol import proactive_chat_response
from assistant_agent.runtime.proactive_delivery import (
    ProactiveDeliveryOwnershipError,
    ProactiveDeliveryStore,
)


class ProactiveDeliveryAckError(ValueError):
    """An ACK does not match this authenticated thread delivery channel."""


class MediaProactiveDeliveryPump:
    """Pull one thread's rows serially while the Media connection is alive."""

    def __init__(
        self,
        *,
        store: ProactiveDeliveryStore,
        user_id: str,
        thread_id: str,
        connection_id: str,
        protocol_session_id: str | None,
        ack_capable: bool,
        sender: Callable[[dict[str, object]], Awaitable[None]],
        ack_timeout_seconds: float,
        lease_seconds: float,
        presence_ttl_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        for name, value in (
            ("ack_timeout_seconds", ack_timeout_seconds),
            ("lease_seconds", lease_seconds),
            ("presence_ttl_seconds", presence_ttl_seconds),
            ("poll_interval_seconds", poll_interval_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.store = store
        self.user_id = user_id
        self.thread_id = thread_id
        self.connection_id = connection_id
        self.protocol_session_id = protocol_session_id
        self.ack_capable = ack_capable
        self.sender = sender
        self.ack_timeout_seconds = ack_timeout_seconds
        self.lease_seconds = lease_seconds
        self.presence_ttl_seconds = presence_ttl_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._opened = False
        self._closed = False
        self._inflight_message_id: str | None = None
        self._ack_events: dict[str, asyncio.Event] = {}
        self._transition_lock = asyncio.Lock()
        self._retry_delay_seconds = 0.0

    async def aopen(self) -> None:
        if self._opened:
            return
        await asyncio.to_thread(
            self.store.register_presence,
            user_id=self.user_id,
            thread_id=self.thread_id,
            connection_id=self.connection_id,
            ttl_seconds=self.presence_ttl_seconds,
        )
        self._opened = True

    async def run(self) -> None:
        await self.aopen()
        try:
            while not self._closed:
                delivered = await self.adeliver_once()
                delay = (
                    self._retry_delay_seconds
                    if delivered and self._retry_delay_seconds > 0
                    else self.poll_interval_seconds
                )
                await asyncio.sleep(delay)
        finally:
            await self.aclose()

    async def adeliver_once(self) -> bool:
        if not self._opened or self._closed:
            raise RuntimeError("proactive delivery pump is not open")
        await asyncio.to_thread(
            self.store.refresh_presence,
            user_id=self.user_id,
            thread_id=self.thread_id,
            connection_id=self.connection_id,
            ttl_seconds=self.presence_ttl_seconds,
        )
        record = await asyncio.to_thread(
            self.store.claim_next,
            user_id=self.user_id,
            thread_id=self.thread_id,
            connection_id=self.connection_id,
            ack_capable=self.ack_capable,
            lease_seconds=self.lease_seconds,
        )
        if record is None:
            return False
        message = record.message
        self._inflight_message_id = message.message_id
        ack_event = asyncio.Event()
        if message.delivery_mode == "durable":
            self._ack_events[message.message_id] = ack_event
        try:
            await self.sender(
                proactive_chat_response(
                    session_id=self.protocol_session_id,
                    message=message,
                )
            )
            if message.delivery_mode == "connection_ephemeral":
                await asyncio.to_thread(
                    self.store.mark_sent_unacknowledged,
                    message_id=message.message_id,
                    user_id=self.user_id,
                    thread_id=self.thread_id,
                    connection_id=self.connection_id,
                )
                self._retry_delay_seconds = 0.0
                return True
            try:
                await asyncio.wait_for(
                    ack_event.wait(),
                    timeout=self.ack_timeout_seconds,
                )
                self._retry_delay_seconds = 0.0
            except TimeoutError:
                async with self._transition_lock:
                    if ack_event.is_set():
                        self._retry_delay_seconds = 0.0
                    else:
                        await asyncio.to_thread(
                            self.store.release,
                            message_id=message.message_id,
                            connection_id=self.connection_id,
                            issue_code="ack_timeout",
                        )
                        self._retry_delay_seconds = min(
                            30.0,
                            self.poll_interval_seconds
                            * (2 ** min(record.attempt_count, 10)),
                        )
            return True
        except asyncio.CancelledError:
            await self._release_inflight("connection_lost")
            raise
        except Exception:
            await self._release_inflight("socket_send_failed")
            raise
        finally:
            self._ack_events.pop(message.message_id, None)
            self._inflight_message_id = None

    async def acknowledge(self, *, chat_index: str, delivery_id: str) -> None:
        if chat_index != f"proactive:{delivery_id}":
            raise ProactiveDeliveryAckError(
                "proactive delivery acknowledgement does not match chatIndex"
            )
        async with self._transition_lock:
            try:
                await asyncio.to_thread(
                    self.store.acknowledge,
                    message_id=delivery_id,
                    user_id=self.user_id,
                    thread_id=self.thread_id,
                    connection_id=self.connection_id,
                )
            except (KeyError, ProactiveDeliveryOwnershipError) as exc:
                raise ProactiveDeliveryAckError(
                    "proactive delivery acknowledgement does not match this connection"
                ) from exc
            event = self._ack_events.get(delivery_id)
            if event is not None:
                event.set()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._release_inflight("connection_lost")
        if self._opened:
            await asyncio.to_thread(
                self.store.unregister_presence,
                thread_id=self.thread_id,
                connection_id=self.connection_id,
            )

    async def _release_inflight(self, issue_code: str) -> None:
        message_id = self._inflight_message_id
        if message_id is None:
            return
        async with self._transition_lock:
            with suppress(KeyError, ProactiveDeliveryOwnershipError):
                await asyncio.to_thread(
                    self.store.release,
                    message_id=message_id,
                    connection_id=self.connection_id,
                    issue_code=issue_code,
                )


__all__ = [
    "MediaProactiveDeliveryPump",
    "ProactiveDeliveryAckError",
]
