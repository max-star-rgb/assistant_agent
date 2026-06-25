"""Agent WebSocket routes backed by graph runtime events."""

import asyncio
import ipaddress
import logging
from threading import Thread
from typing import Any

from fastapi import APIRouter, Query, WebSocket

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.assistant_run_service import run_assistant_request


router = APIRouter()
logger = logging.getLogger("multimodal_agent.api.websocket")


def _preview(text: str | None, *, limit: int = 80) -> str:
    """Return a one-line, length-bounded preview for console logs."""

    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def get_agent_runtime() -> AgentGraphRuntime:
    """Reuse the HTTP singleton runtime so WS and HTTP share trace_store."""

    from multimodal_agent.api import routes_agent

    return routes_agent.get_agent_runtime()


def get_trial_access_gate():
    from multimodal_agent.api import routes_agent

    return routes_agent.get_trial_access_gate()


class WebSocketEventSink:
    """Forward runtime events to an asyncio queue from a worker thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Any]) -> None:
        self.loop = loop
        self.queue = queue

    def emit(self, event: AgentEvent) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)


def mock_agent_events(session_id: str) -> list[AgentEvent]:
    """Return legacy fallback WebSocket events for compatibility tests."""

    return [
        AgentEvent(type="tool_started", session_id=session_id, tool_name="mock_tool"),
        AgentEvent(type="tool_progress", session_id=session_id, tool_name="mock_tool", progress=0.5),
        AgentEvent(type="tool_completed", session_id=session_id, tool_name="mock_tool", output_ref="mock://tool/result"),
        AgentEvent(type="agent_response", session_id=session_id, text="mock response"),
    ]


@router.websocket("/ws/agent/{session_id}")
async def agent_websocket(
    websocket: WebSocket,
    session_id: str,
    text: str = Query(...),
    user_id: str = Query(default="web_chat_user"),
    client_kind: str = Query(default="web", alias="client"),
    image_id: list[str] | None = Query(default=None),
    video_id: list[str] | None = Query(default=None),
) -> None:
    access = get_trial_access_gate().check(user_id)
    if not access.allowed and not _can_bypass_trial_access(websocket, client_kind):
        await websocket.accept()
        await websocket.send_json(
            AgentEvent(
                type="agent_error",
                session_id=session_id,
                error={
                    "code": "ACCESS_DENIED",
                    "message": access.reason or "trial user is not allowed",
                    "detail": {"user_id": access.user_id},
                    "recoverable": True,
                },
            ).model_dump(mode="json", exclude_none=True)
        )
        await websocket.close(code=1008)
        return
    await websocket.accept()
    logger.info("[ws] session=%s user=%s 收到: %s", session_id, user_id, _preview(text))
    request = UserRequest(
        user_id=user_id,
        session_id=session_id,
        text=text,
        image_ids=list(image_id or []),
        video_ids=list(video_id or []),
        metadata={"source": _request_source(client_kind), "transport": "websocket"},
    )
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    event_sink = WebSocketEventSink(loop, queue)

    def run_agent() -> None:
        try:
            runtime = get_agent_runtime()
            artifacts = run_assistant_request(request, runtime=runtime, event_sink=event_sink)
            response = artifacts.api_response()
            logger.info(
                "[ws] session=%s run=%s status=%s 返回: %s",
                session_id,
                response.run_id,
                response.status,
                _preview(response.response_text),
            )
            event_sink.emit(
                AgentEvent(
                    type="agent_response",
                    session_id=session_id,
                    run_id=response.run_id,
                    text=response.response_text,
                    payload={"response": response.model_dump(mode="json")},
                )
            )
        except Exception as exc:
            logger.exception("[ws] session=%s 运行失败: %s", session_id, exc)
            event_sink.emit(
                AgentEvent(
                    type="agent_error",
                    session_id=session_id,
                    error={"code": "TASK_FAILED", "message": str(exc), "detail": {}, "recoverable": False},
                )
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    worker = Thread(target=run_agent, daemon=True)
    worker.start()
    while True:
        event = await queue.get()
        if event is None:
            break
        await websocket.send_json(event.model_dump(mode="json", exclude_none=True))
    worker.join(timeout=1)
    await websocket.close()


def _can_bypass_trial_access(websocket: WebSocket, client_kind: str) -> bool:
    if client_kind != "cli":
        return False
    host = websocket.client.host if websocket.client is not None else ""
    return _is_local_client_host(host)


def _is_local_client_host(host: str) -> bool:
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_source(client_kind: str) -> str:
    return "cli_client" if client_kind == "cli" else "web_console"
