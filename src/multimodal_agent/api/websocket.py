"""Agent WebSocket routes backed by graph runtime events."""

import asyncio
import ipaddress
import logging
from threading import Thread
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.api_identity import resolve_request_identity
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
        if self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except RuntimeError:
            logger.debug("websocket event loop closed before event could be emitted", exc_info=True)


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
    text: str | None = Query(default=None),
    user_id: str = Query(default="web_chat_user"),
    client_kind: str = Query(default="web", alias="client"),
    image_id: list[str] | None = Query(default=None),
    video_id: list[str] | None = Query(default=None),
    execution_strategy: str = Query(default="react"),
) -> None:
    identity_resolution = resolve_request_identity(
        user_id=user_id,
        session_id=session_id,
        source="websocket_query",
    )
    identity = identity_resolution.identity
    access = identity_resolution.trial_access(get_trial_access_gate())
    if not access.allowed and not _can_bypass_trial_access(websocket, client_kind):
        await websocket.accept()
        await websocket.send_json(
            AgentEvent(
                type="agent_error",
                session_id=session_id,
                error={
                    "code": "ACCESS_DENIED",
                    "message": access.reason or "trial user is not allowed",
                    "detail": {"user_id": identity.user_id},
                    "recoverable": True,
                },
            ).model_dump(mode="json", exclude_none=True)
        )
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        request_payload = await _read_request_payload(
            websocket,
            text=text,
            image_ids=list(image_id or []),
            video_ids=list(video_id or []),
            execution_strategy=execution_strategy,
        )
    except WebSocketDisconnect:
        return
    except ValueError as exc:
        await websocket.send_json(
            AgentEvent(
                type="agent_error",
                session_id=session_id,
                error={"code": "BAD_REQUEST", "message": str(exc), "detail": {}, "recoverable": True},
            ).model_dump(mode="json", exclude_none=True)
        )
        await websocket.close(code=1003)
        return

    request_text = request_payload["text"]
    request_execution_strategy = request_payload["execution_strategy"]
    logger.info("[ws] session=%s user=%s 收到: %s", session_id, identity.user_id, _preview(request_text))
    request = UserRequest(
        user_id=identity.user_id,
        session_id=identity.session_id or session_id,
        text=request_text,
        image_ids=request_payload["image_ids"],
        video_ids=request_payload["video_ids"],
        execution_strategy=_execution_strategy(request_execution_strategy),
        metadata={
            "source": _request_source(client_kind),
            "transport": "websocket",
            "execution_strategy": _execution_strategy(request_execution_strategy),
            "request_identity": identity_resolution.metadata(),
        },
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
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                except RuntimeError:
                    logger.debug("websocket event loop closed before completion signal", exc_info=True)

    worker = Thread(target=run_agent, daemon=True)
    worker.start()
    while True:
        event = await queue.get()
        if event is None:
            break
        await websocket.send_json(event.model_dump(mode="json", exclude_none=True))
    worker.join(timeout=1)
    await websocket.close()


async def _read_request_payload(
    websocket: WebSocket,
    *,
    text: str | None,
    image_ids: list[str],
    video_ids: list[str],
    execution_strategy: str,
) -> dict[str, Any]:
    if text is not None:
        return {
            "text": text,
            "image_ids": image_ids,
            "video_ids": video_ids,
            "execution_strategy": execution_strategy,
        }

    try:
        payload = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except TimeoutError as exc:
        raise ValueError("WebSocket request payload was not received.") from exc
    except WebSocketDisconnect:
        raise
    except Exception as exc:
        raise ValueError("Invalid WebSocket request payload.") from exc

    request_text = payload.get("text")
    if not isinstance(request_text, str) or not request_text.strip():
        raise ValueError("WebSocket request text is required.")
    return {
        "text": request_text,
        "image_ids": _payload_id_list(payload.get("image_ids"), fallback=image_ids),
        "video_ids": _payload_id_list(payload.get("video_ids"), fallback=video_ids),
        "execution_strategy": str(payload.get("execution_strategy") or execution_strategy),
    }


def _payload_id_list(value: Any, *, fallback: list[str]) -> list[str]:
    if value is None:
        return fallback
    if not isinstance(value, list):
        return fallback
    return [str(item).strip() for item in value if str(item).strip()]


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


def _execution_strategy(value: str) -> str:
    return "plan_and_solve" if value == "plan_and_solve" else "react"
