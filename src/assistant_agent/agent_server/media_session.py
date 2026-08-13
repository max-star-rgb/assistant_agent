"""Minimal Media connection correlation over Agent Server resources."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MediaConnectionSession:
    connection_id: str
    protocol_session_id: str | None = None
    user_id: str | None = None
    thread_id: str | None = None
    active_runs: dict[str, str] = field(default_factory=dict)
    deliveries: dict[str, str] = field(default_factory=dict)
    last_event_id: str | None = None
    client_capabilities: dict[str, bool] = field(default_factory=dict)
    media_capabilities: tuple[str, ...] = ()

    def bind_control(
        self,
        *,
        protocol_session_id: str | None,
        user_id: str,
        thread_id: str,
        client_capabilities: dict[str, bool] | None = None,
        media_capabilities: tuple[str, ...] = (),
    ) -> None:
        if self.thread_id is not None:
            raise ValueError("assistantControl already bound this connection")
        self.protocol_session_id = protocol_session_id
        self.user_id = user_id
        self.thread_id = thread_id
        self.client_capabilities = dict(client_capabilities or {})
        self.media_capabilities = media_capabilities

    def bind_run(self, *, chat_index: str, run_id: str) -> None:
        existing = self.active_runs.get(chat_index)
        if existing is not None and existing != run_id:
            raise ValueError("chatIndex already maps to another native run")
        self.active_runs[chat_index] = run_id

    def bind_delivery(self, *, delivery_id: str, chat_index: str) -> None:
        self.deliveries[delivery_id] = chat_index

    def acknowledge(self, *, delivery_id: str, chat_index: str) -> None:
        if self.deliveries.get(delivery_id) != chat_index:
            raise ValueError("delivery acknowledgement does not match chatIndex")
        self.deliveries.pop(delivery_id, None)

    def active_run_targets(self) -> tuple[tuple[str, str], ...]:
        if self.thread_id is None:
            return ()
        return tuple((self.thread_id, run_id) for run_id in self.active_runs.values())

    def finish_run(self, *, chat_index: str) -> None:
        self.active_runs.pop(chat_index, None)


__all__ = ["MediaConnectionSession"]
