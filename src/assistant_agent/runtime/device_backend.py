"""Opt-in device execution experiment; no Agent/run lifecycle is implemented here."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hmac
import json
from typing import Any, Literal
from uuid import uuid4

from deepagents.backends.protocol import (
    DeleteResult, ExecuteResponse, GlobResult, GrepResult, LsResult,
    ReadResult, SandboxBackendProtocol, WriteResult, EditResult,
    FileDownloadResponse, FileUploadResponse,
)
from langgraph.runtime import get_runtime
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from assistant_agent.native_agent.context import authenticated_user_identity


MAX_FRAME_BYTES = 1_048_576
Operation = Literal["ls", "read", "write", "edit", "delete", "glob", "grep", "execute",
                    "read_bytes", "write_bytes", "abort_upload", "git", "browser", "instructions"]


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["call", "cancel"]
    request_id: str = Field(min_length=1, max_length=64)
    operation: Operation | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class DeviceReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["result"] = "result"
    request_id: str = Field(min_length=1, max_length=64)
    result: dict[str, Any] | None = None
    error: Literal[
        "invalid_request", "file_operation_failed", "shell_disabled",
        "operation_failed", "duplicate_request", "client_busy",
        "execution_timeout", "cancelled", "output_limit",
    ] | None = None


class DeviceOperationError(RuntimeError):
    """Bounded transport error; remote traceback/host paths never leak."""


@dataclass
class _Connection:
    socket: ServerConnection
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class DeviceBroker:
    """Reverse WebSocket transport, with grants supplied by trusted composition.

    Keys are (authenticated owner, device). Tokens never enter graph context.
    This is process-local and deliberately not registered by production composition.
    """

    def __init__(self, grants: dict[tuple[str, str], str]) -> None:
        if any(not all(key) or len(token) < 32 or not token.isascii() for key, token in grants.items()):
            raise ValueError("device grants require owner, device and a strong token")
        if len(set(grants.values())) != len(grants):
            raise ValueError("device tokens must be unique")
        self._grants = dict(grants)
        self._connections: dict[tuple[str, str], _Connection] = {}
        self._changed = asyncio.Condition()

    async def wait_connected(self, owner: str, device: str) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: (owner, device) in self._connections)

    def grant(self, owner: str, device: str, token: str) -> None:
        if (owner, device) in self._grants:
            raise ValueError("device already registered")
        self._grants[(owner, device)] = token

    def revoke(self, owner: str, device: str) -> None:
        self._grants.pop((owner, device), None)

    def is_connected(self, owner: str, device: str) -> bool:
        return (owner, device) in self._connections

    async def disconnect(self, owner: str, device: str) -> None:
        connection = self._connections.get((owner, device))
        if connection is not None:
            await connection.socket.close(code=1000, reason="client_unregistered")

    async def wait_disconnected(self, owner: str, device: str) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: (owner, device) not in self._connections)

    async def accept(self, socket: ServerConnection) -> None:
        """Pass to websockets.serve with max_size=MAX_FRAME_BYTES and TLS remotely."""
        try:
            header = socket.request.headers.get("Authorization", "")
            token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
            key = next((key for key, secret in self._grants.items()
                        if token.isascii() and hmac.compare_digest(secret, token)), None)
        except (ValueError, TypeError):
            key = None
        if key is None or key in self._connections:
            await socket.close(code=1008, reason="device_auth_rejected")
            return
        connection = _Connection(socket)
        async with self._changed:
            self._connections[key] = connection
            self._changed.notify_all()
        try:
            await socket.send(json.dumps({"type": "ready", "device": key[1]}))
            async for raw in socket:
                if len(raw.encode() if isinstance(raw, str) else raw) > MAX_FRAME_BYTES:
                    raise ValueError("device response too large")
                reply = DeviceReply.model_validate_json(raw)
                future = connection.pending.get(reply.request_id)
                if future is not None and not future.done():
                    if reply.error:
                        future.set_exception(DeviceOperationError(reply.error))
                    elif reply.result is not None:
                        future.set_result(reply.result)
                    else:
                        future.set_exception(DeviceOperationError("invalid_device_response"))
        except (ConnectionClosed, ValueError):
            await socket.close(code=1008, reason="device_disconnected")
        finally:
            async with self._changed:
                self._connections.pop(key, None)
                self._changed.notify_all()
            for future in connection.pending.values():
                if not future.done():
                    future.set_exception(DeviceOperationError("device_disconnected_outcome_unknown"))

    async def call(self, owner: str, device: str, operation: Operation,
                   arguments: dict[str, Any], *, deadline: float = 125) -> dict[str, Any]:
        """Trusted server API. No retry: lost responses may already have side effects."""
        connection = self._connections.get((owner, device))
        if connection is None:
            raise DeviceOperationError("device_unavailable")
        if len(connection.pending) >= 8:
            raise DeviceOperationError("device_busy")
        request = DeviceRequest(type="call", request_id=uuid4().hex,
                                operation=operation, arguments=arguments)
        payload = request.model_dump_json()
        if len(payload.encode()) > MAX_FRAME_BYTES:
            raise DeviceOperationError("request_too_large")
        future = asyncio.get_running_loop().create_future()
        connection.pending[request.request_id] = future
        try:
            await connection.socket.send(payload)
            return await asyncio.wait_for(future, deadline)
        except (asyncio.CancelledError, TimeoutError):
            try:
                await connection.socket.send(DeviceRequest(
                    type="cancel", request_id=request.request_id,
                ).model_dump_json())
            except ConnectionClosed:
                pass
            if asyncio.current_task().cancelling():
                raise
            raise DeviceOperationError("device_timeout_outcome_unknown") from None
        except ConnectionClosed:
            raise DeviceOperationError("device_disconnected_outcome_unknown") from None
        finally:
            connection.pending.pop(request.request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume an error racing with socket closure.


class DeviceBackend(SandboxBackendProtocol):
    """Async native backend. The protocol name does NOT imply a local OS sandbox.

    Device selection is trusted composition; owner comes from native ServerInfo.
    Virtual filesystem paths are relative to the client's approved root. Shell
    commands use that root as cwd, not as an OS security boundary.
    """

    def __init__(self, broker: DeviceBroker, device: str, *, owner: str | None = None, subdirectory: str | None = None) -> None:
        self._broker = broker
        self._device = device
        self._owner = owner
        self._subdirectory = subdirectory

    async def call(self, operation, arguments):
        if self._subdirectory:
            arguments = {**arguments, "_subdirectory": self._subdirectory}
        owner = self._owner or authenticated_user_identity(get_runtime())
        return await self._broker.call(owner, self._device, operation, arguments)

    @property
    def id(self) -> str:
        return f"device:{self._device}"

    async def _invoke(self, operation, arguments, result_type):
        try:
            result = await self.call(operation, arguments)
            return TypeAdapter(result_type).validate_python(result)
        except DeviceOperationError as exc:
            if result_type is ExecuteResponse:
                return ExecuteResponse(output=str(exc), exit_code=1)
            return result_type(error=str(exc))

    async def als(self, path):
        return await self._invoke("ls", {"path": path}, LsResult)

    async def aread(self, file_path, offset=0, limit=2000):
        return await self._invoke("read", dict(file_path=file_path, offset=offset, limit=limit), ReadResult)

    async def awrite(self, file_path, content):
        return await self._invoke("write", dict(file_path=file_path, content=content), WriteResult)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return await self._invoke("edit", dict(file_path=file_path, old_string=old_string,
                                             new_string=new_string, replace_all=replace_all), EditResult)

    async def adelete(self, file_path):
        return await self._invoke("delete", {"file_path": file_path}, DeleteResult)

    async def aglob(self, pattern, path=None):
        return await self._invoke("glob", dict(pattern=pattern, path=path), GlobResult)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return await self._invoke("grep", dict(pattern=pattern, path=path, glob=glob, max_count=max_count), GrepResult)

    async def aexecute(self, command, *, timeout=None):
        return await self._invoke("execute", dict(command=command, timeout=timeout), ExecuteResponse)

    async def download(self, path):
        from assistant_agent.runtime.device_files import MAX_BINARY_BYTES
        data, version = bytearray(), None
        try:
            while True:
                reply = await self.call("read_bytes", {"file_path": path, "offset": len(data), "version": version})
                chunk = base64.b64decode(reply["content"], validate=True)
                data.extend(chunk)
                if len(data) > MAX_BINARY_BYTES or (not chunk and not reply["final"]):
                    raise ValueError("invalid binary transfer")
                version = reply["version"]
                if reply["final"]:
                    return FileDownloadResponse(path=path, content=bytes(data))
        except (DeviceOperationError, ValueError, KeyError) as exc:
            return FileDownloadResponse(path=path, error=str(exc))

    async def upload(self, path, data):
        from assistant_agent.runtime.device_files import CHUNK_BYTES, MAX_BINARY_BYTES
        if len(data) > MAX_BINARY_BYTES:
            return FileUploadResponse(path=path, error="file too large")
        transfer_id = uuid4().hex
        try:
            for offset in range(0, max(1, len(data)), CHUNK_BYTES):
                chunk = data[offset:offset + CHUNK_BYTES]
                await self.call("write_bytes", {"file_path": path, "transfer_id": transfer_id,
                    "offset": offset, "content": base64.b64encode(chunk).decode(),
                    "final": offset + len(chunk) == len(data)})
            return FileUploadResponse(path=path)
        except DeviceOperationError as exc:
            return FileUploadResponse(path=path, error=str(exc))
        finally:
            try:
                await self.call("abort_upload", {"transfer_id": transfer_id})
            except DeviceOperationError:
                pass

    async def adownload_files(self, paths):
        return [await self.download(path) for path in paths]

    async def aupload_files(self, files):
        return [await self.upload(path, data) for path, data in files]


def device_backend_for_runtime(runtime):
    from assistant_agent.agent_server.devices import local_devices
    owner = authenticated_user_identity(runtime)
    local_devices.require_binding(owner, runtime.context, connected=True)
    return DeviceBackend(local_devices.broker, runtime.context.device_id, owner=owner)
