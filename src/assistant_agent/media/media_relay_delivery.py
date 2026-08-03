"""Deliver rendering payloads through the active Media-Service WebSocket relay."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any


MediaRelaySender = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class MediaRelayConnection:
    """An Agent-to-Media connection used to relay payloads to RenderingClient."""

    connection_id: str
    number: str
    send: MediaRelaySender


class MediaRelayConnectionRegistry:
    """Map callback session ids to the active Media-Service relay connection."""

    def __init__(self) -> None:
        self._connections: dict[str, MediaRelayConnection] = {}

    def register(
        self,
        *,
        connection_id: str,
        session_ids: Iterable[str],
        number: str,
        send: MediaRelaySender,
    ) -> None:
        connection = MediaRelayConnection(
            connection_id=connection_id,
            number=number,
            send=send,
        )
        for session_id in session_ids:
            normalized = session_id.strip()
            if normalized:
                self._connections[normalized] = connection

    def unregister(self, connection_id: str) -> None:
        stale = [
            session_id
            for session_id, connection in self._connections.items()
            if connection.connection_id == connection_id
        ]
        for session_id in stale:
            self._connections.pop(session_id, None)

    async def deliver_3d_result(
        self,
        *,
        session_id: str,
        chat_index: str,
        media_type: str,
        model_url: str,
    ) -> bool:
        """Send a 3D result to Media Service for downstream renderer forwarding."""

        connection = self._connections.get(session_id)
        if connection is None:
            return False
        description = f"{media_type.upper()} 3D生成完成"
        detail = (
            {"type": "VIDEO", "videoUrl": model_url}
            if media_type == "mp4"
            else {"type": "TD_MODEL", "modelUrl": model_url}
        )
        body = {
            "number": connection.number,
            "message": {
                "type": "BRIEF",
                "chatIndex": chat_index,
                "content": {
                    "intentResult": {
                        "description": description,
                        "status": "SUCCESS",
                        "detail": [detail],
                    }
                },
            },
        }
        try:
            await connection.send(
                {
                    "message": "chatResponse",
                    "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                }
            )
        except Exception:  # noqa: BLE001 - transport failure becomes callback failure.
            self.unregister(connection.connection_id)
            return False
        return True


media_relay_connection_registry = MediaRelayConnectionRegistry()
