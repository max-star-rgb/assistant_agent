"""Synchronous turn facade over Gateway session lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.gateway.protocol import Frame, frame
from assistant_agent.gateway.session import GatewaySessionManager


class GatewayTurnError(RuntimeError):
    """Raised when a Gateway turn cannot reach a terminal frame."""


class GatewayTurnTimeout(GatewayTurnError):
    """Raised when a Gateway turn does not finish before the caller timeout."""


@dataclass(frozen=True)
class GatewayTurnRequest:
    """Input for one Gateway-normalized user turn."""

    user_id: str
    session_id: str
    text: str
    image_ids: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    audio_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 30.0


@dataclass(frozen=True)
class GatewayTurnResult:
    """Collected result for one Gateway turn."""

    frames: list[Frame]
    terminal_frame: Frame
    response_text: str
    status: str
    reason: str
    run_id: str | None
    turn_id: str | None
    trace_id: str | None
    payload: dict[str, Any]


class GatewayTurnFacade:
    """Run one request/response turn through Gateway frame semantics."""

    def __init__(self, *, manager: GatewaySessionManager) -> None:
        self._manager = manager

    async def run_turn(self, request: GatewayTurnRequest) -> GatewayTurnResult:
        if not request.user_id:
            raise ValueError("GatewayTurnRequest.user_id is required")
        if not request.session_id:
            raise ValueError("GatewayTurnRequest.session_id is required")
        if request.timeout_s <= 0:
            raise ValueError("GatewayTurnRequest.timeout_s must be positive")

        handle = await self._manager.acquire(
            user_id=request.user_id,
            config=request.config,
        )
        turn_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        await handle.endpoint.send(
            frame(
                type="message.user",
                session_id=request.session_id,
                user_id=request.user_id,
                payload=_message_payload(request, turn_id=turn_id, run_id=run_id),
            )
        )
        return await self._collect_turn(
            handle.endpoint,
            session_id=request.session_id,
            turn_id=turn_id,
            run_id=run_id,
            timeout_s=request.timeout_s,
        )

    async def _collect_turn(
        self,
        endpoint,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        timeout_s: float,
    ) -> GatewayTurnResult:
        frames: list[Frame] = []
        chunks: list[str] = []

        async def _read_until_terminal() -> GatewayTurnResult:
            async for received in endpoint:
                if not _matches_turn(
                    received,
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                ):
                    continue
                frames.append(received)
                if received.get("type") == "stream.chunk":
                    chunks.append(_chunk_text(received))
                    continue
                if received.get("type") == "run.end":
                    return _turn_result(frames=frames, terminal=received, chunks=chunks)
            raise GatewayTurnError("Gateway endpoint closed before run.end")

        try:
            return await asyncio.wait_for(_read_until_terminal(), timeout=timeout_s)
        except TimeoutError as exc:
            raise GatewayTurnTimeout(
                f"Gateway turn timed out after {timeout_s:.3g}s before run.end"
            ) from exc


def _message_payload(
    request: GatewayTurnRequest,
    *,
    turn_id: str,
    run_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": request.text,
        "turn_id": turn_id,
        "run_id": run_id,
    }
    if request.image_ids:
        payload["image_ids"] = list(request.image_ids)
    if request.video_ids:
        payload["video_ids"] = list(request.video_ids)
    if request.audio_id is not None:
        payload["audio_id"] = request.audio_id
    if request.metadata:
        payload["metadata"] = dict(request.metadata)
    return payload


def _matches_turn(
    received: Mapping[str, Any],
    *,
    session_id: str,
    turn_id: str,
    run_id: str,
) -> bool:
    if received.get("session_id") != session_id:
        return False
    if received.get("turn_id") not in {None, turn_id}:
        return False
    if received.get("run_id") not in {None, run_id}:
        return False
    return True


def _chunk_text(received: Mapping[str, Any]) -> str:
    payload = received.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _turn_result(
    *,
    frames: list[Frame],
    terminal: Frame,
    chunks: list[str],
) -> GatewayTurnResult:
    payload = _payload_dict(terminal)
    reason = str(terminal.get("reason") or "")
    return GatewayTurnResult(
        frames=list(frames),
        terminal_frame=dict(terminal),
        response_text="".join(chunks),
        status=_status_from_terminal(terminal),
        reason=reason,
        run_id=terminal.get("run_id"),
        turn_id=terminal.get("turn_id"),
        trace_id=_optional_string(payload.get("trace_id")),
        payload=payload,
    )


def _payload_dict(received: Mapping[str, Any]) -> dict[str, Any]:
    payload = received.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _status_from_terminal(terminal: Mapping[str, Any]) -> str:
    reason = terminal.get("reason")
    if reason in {"completed", "cancelled", "error"}:
        return str(reason)
    if terminal.get("error") is not None:
        return "error"
    return "completed"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
