"""Same-host device connections for the existing Agent Server custom app.

This pilot shares the trusted local developer identity policy. It is deliberately
closed to remote hosts and browsers until account authentication is deployed.
"""
from __future__ import annotations

from pathlib import Path
import secrets
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from websockets.exceptions import ConnectionClosed

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.runtime.device_backend import DeviceBroker


def require_loopback(request) -> None:
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or request.headers.get("origin")):
        raise HTTPException(403, "device pilot accepts only same-host native clients")


class LocalDevices:
    def __init__(self):
        self.broker = DeviceBroker({})
        self.bindings: dict[tuple[str, str], Path] = {}

    def register(self, owner: str, device: str, cwd: Path) -> str:
        token = secrets.token_urlsafe(32)
        self.broker.grant(owner, device, token)
        self.bindings[(owner, device)] = cwd
        return token

    def require_binding(self, owner: str, context: AssistantRunContext, *, connected=False) -> None:
        if self.bindings.get((owner, context.device_id)) != context.cwd:
            raise ValueError("device is not registered for this identity and cwd")
        if connected and not self.broker.is_connected(owner, context.device_id):
            raise ValueError("device is offline")

    def remove(self, owner: str, device: str):
        self.broker.revoke(owner, device)
        self.bindings.pop((owner, device), None)


local_devices = LocalDevices()
router = APIRouter()


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    cwd: str


@router.post("/local-devices")
async def register_device(request: Request, registration: Registration):
    require_loopback(request)
    context = AssistantRunContext(cwd=registration.cwd, device_id=registration.device_id)
    owner = request.user.identity
    try:
        token = local_devices.register(owner, registration.device_id, context.cwd)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"token": token, "device_id": registration.device_id, "identity": owner}


@router.delete("/local-devices/{device_id}")
async def remove_device(request: Request, device_id: str):
    require_loopback(request)
    await local_devices.broker.disconnect(request.user.identity, device_id)
    local_devices.remove(request.user.identity, device_id)
    return {"removed": True}


class _Socket:
    """Translate Starlette transport to the existing broker's socket surface."""
    def __init__(self, socket: WebSocket):
        self.socket = socket
        self.request = SimpleNamespace(headers=socket.headers)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.socket.receive_text()
        except WebSocketDisconnect:
            raise StopAsyncIteration from None

    async def send(self, data):
        try:
            await self.socket.send_text(data)
        except (WebSocketDisconnect, RuntimeError, OSError):
            raise ConnectionClosed(None, None) from None

    async def close(self, code=1000, reason=""):
        try:
            await self.socket.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect):
            pass


@router.websocket("/local-devices/connect")
async def connect_device(socket: WebSocket):
    try:
        require_loopback(socket)
    except HTTPException:
        await socket.close(code=1008)
        return
    await socket.accept()
    # Credential validation remains solely in the broker, not URL parameters.
    await local_devices.broker.accept(_Socket(socket))
