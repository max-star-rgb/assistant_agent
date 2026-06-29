"""Minimal A2A JSON-RPC protocol schemas for inbound adapter routes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


A2A_PROTOCOL_VERSION = "1.0.0"
A2A_JSONRPC_VERSION = "2.0"
A2A_SEND_MESSAGE_METHODS = {"SendMessage", "message/send"}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class A2AJsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request accepted by the inbound A2A adapter."""

    jsonrpc: Literal["2.0"] = A2A_JSONRPC_VERSION
    id: str | int | None = None
    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class A2AJsonRpcError(BaseModel):
    """JSON-RPC error object."""

    code: int
    message: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class A2AJsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response."""

    jsonrpc: Literal["2.0"] = A2A_JSONRPC_VERSION
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: A2AJsonRpcError | None = None
