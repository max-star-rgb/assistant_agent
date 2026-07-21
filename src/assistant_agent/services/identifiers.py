"""Central identifier generation for runtime and observability boundaries."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from threading import Lock
from uuid import UUID


UUID7_RANDOM_BITS = 74
UUID7_RANDOM_MASK = (1 << UUID7_RANDOM_BITS) - 1
UUID7_TIMESTAMP_MASK = (1 << 48) - 1
W3C_TRACE_ID_BITS = 128
W3C_SPAN_ID_BITS = 64


class IdFactory:
    """Generate opaque IDs with sortable UUIDv7 business identifiers."""

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        randbits: Callable[[int], int] | None = None,
    ) -> None:
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._randbits = randbits or secrets.randbits
        self._lock = Lock()
        self._last_pid = os.getpid()
        self._last_timestamp_ms = -1
        self._last_random = -1

    def uuid7(self) -> UUID:
        """Return a process-monotonic RFC 9562 UUIDv7."""

        with self._lock:
            current_pid = os.getpid()
            if current_pid != self._last_pid:
                self._last_pid = current_pid
                self._last_timestamp_ms = -1
                self._last_random = -1
            timestamp_ms = max(0, int(self._clock_ms()))
            if timestamp_ms > UUID7_TIMESTAMP_MASK:
                raise OverflowError("UUIDv7 timestamp exceeds 48 bits")
            if timestamp_ms > self._last_timestamp_ms:
                random_payload = self._randbits(UUID7_RANDOM_BITS) & UUID7_RANDOM_MASK
            else:
                timestamp_ms = self._last_timestamp_ms
                random_payload = self._last_random + 1
                if random_payload > UUID7_RANDOM_MASK:
                    timestamp_ms += 1
                    if timestamp_ms > UUID7_TIMESTAMP_MASK:
                        raise OverflowError("UUIDv7 timestamp exceeds 48 bits")
                    random_payload = self._randbits(UUID7_RANDOM_BITS) & UUID7_RANDOM_MASK
            self._last_timestamp_ms = timestamp_ms
            self._last_random = random_payload

        rand_a = random_payload >> 62
        rand_b = random_payload & ((1 << 62) - 1)
        value = (
            (timestamp_ms << 80)
            | (0x7 << 76)
            | (rand_a << 64)
            | (0b10 << 62)
            | rand_b
        )
        return UUID(int=value)

    def uuid7_hex(self) -> str:
        return self.uuid7().hex

    def uuid7_string(self) -> str:
        return str(self.uuid7())

    def prefixed_uuid7(self, prefix: str, *, separator: str = "_") -> str:
        if not prefix or not prefix.strip():
            raise ValueError("prefix is required")
        return f"{prefix}{separator}{self.uuid7_hex()}"

    def trace_id(self) -> str:
        return _nonzero_hex(self._randbits, W3C_TRACE_ID_BITS)

    def span_id(self) -> str:
        return _nonzero_hex(self._randbits, W3C_SPAN_ID_BITS)


def _nonzero_hex(randbits: Callable[[int], int], bits: int) -> str:
    value = randbits(bits) & ((1 << bits) - 1)
    if value == 0:
        value = 1
    return f"{value:0{bits // 4}x}"


_DEFAULT_ID_FACTORY = IdFactory()


def new_trace_id() -> str:
    return _DEFAULT_ID_FACTORY.trace_id()


def new_span_id() -> str:
    return _DEFAULT_ID_FACTORY.span_id()


def new_run_id() -> str:
    return _DEFAULT_ID_FACTORY.prefixed_uuid7("run")


def new_session_id() -> str:
    return _DEFAULT_ID_FACTORY.prefixed_uuid7("session")


def new_tool_call_id() -> str:
    return _DEFAULT_ID_FACTORY.prefixed_uuid7("call")


def new_turn_id() -> str:
    return _DEFAULT_ID_FACTORY.prefixed_uuid7("turn")


def new_delivery_id() -> str:
    return _DEFAULT_ID_FACTORY.prefixed_uuid7("delivery")


def new_uuid7_string() -> str:
    return _DEFAULT_ID_FACTORY.uuid7_string()


def new_prefixed_uuid7(prefix: str, *, separator: str = "_") -> str:
    return _DEFAULT_ID_FACTORY.prefixed_uuid7(prefix, separator=separator)
