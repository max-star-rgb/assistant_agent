"""Minimal A2A JSON-RPC protocol schemas for inbound adapter routes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "jsonrpc": "2.0",
                    "id": "rpc_1",
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "role": "user",
                            "messageId": "msg_1",
                            "contextId": "demo_session",
                            "parts": [{"kind": "text", "text": "Hello"}],
                            "metadata": {
                                "user_id": "demo_user",
                                "target_agent_id": "agent.worker",
                            },
                        }
                    },
                }
            ]
        }
    )

    jsonrpc: Literal["2.0"] = A2A_JSONRPC_VERSION
    id: str | int | None = None
    method: str = Field(min_length=1)
    params: Any = Field(default_factory=dict)


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


class A2AMessagePart(BaseModel):
    """A minimal public A2A message/artifact part."""

    kind: str = Field(min_length=1)
    text: str | None = None
    file: dict[str, Any] | None = None


class A2AMessage(BaseModel):
    """A minimal A2A message shape used in inbound adapter results."""

    kind: Literal["message"] = "message"
    role: str = Field(min_length=1)
    messageId: str = Field(min_length=1)
    contextId: str = Field(min_length=1)
    taskId: str | None = None
    parts: list[dict[str, Any]] = Field(default_factory=list)


class A2AArtifact(BaseModel):
    """A minimal A2A artifact shape for final outputs."""

    artifactId: str = Field(min_length=1)
    name: str = Field(min_length=1)
    parts: list[dict[str, Any]] = Field(default_factory=list)


class A2ATaskStatus(BaseModel):
    """A2A task status returned by the inbound adapter."""

    state: Literal["completed", "failed", "cancelled"]
    message: A2AMessage


class A2ATaskResult(BaseModel):
    """Task-like result returned by inbound SendMessage."""

    id: str = Field(min_length=1)
    contextId: str = Field(min_length=1)
    kind: Literal["task"] = "task"
    status: A2ATaskStatus
    artifacts: list[A2AArtifact] = Field(default_factory=list)
    history: list[A2AMessage] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2AAgentSkill(BaseModel):
    """Public skill entry exposed in the local agent card."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    inputModes: list[str] = Field(default_factory=list)
    outputModes: list[str] = Field(default_factory=list)


class A2AAgentCard(BaseModel):
    """Public A2A agent card served from the well-known endpoint."""

    protocolVersion: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    url: str = Field(min_length=1)
    preferredTransport: str = Field(min_length=1)
    additionalInterfaces: list[dict[str, Any]] = Field(default_factory=list)
    version: str = Field(min_length=1)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    defaultInputModes: list[str] = Field(default_factory=list)
    defaultOutputModes: list[str] = Field(default_factory=list)
    skills: list[A2AAgentSkill] = Field(default_factory=list)
    supportedMethods: list[str] = Field(default_factory=list)
    authentication: dict[str, Any] = Field(default_factory=dict)
    securitySchemes: dict[str, Any] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] = Field(default_factory=list)
    supportsAuthenticatedExtendedCard: bool = False
