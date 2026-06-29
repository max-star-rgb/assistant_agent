"""Inbound A2A-compatible HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from multimodal_agent.api.routes_agent import get_agent_gateway, get_trial_access_gate
from multimodal_agent.schemas.a2a import (
    A2A_JSONRPC_VERSION,
    A2A_SEND_MESSAGE_METHODS,
    A2AJsonRpcRequest,
    A2AJsonRpcResponse,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
)
from multimodal_agent.services.a2a_adapter import (
    A2AInvalidParams,
    build_agent_card,
    gateway_request_from_a2a_params,
    task_from_gateway_response,
)


router = APIRouter()


@router.get("/.well-known/agent-card.json")
def get_agent_card(request: Request) -> dict[str, Any]:
    """Expose a local A2A agent card for discovery."""

    return build_agent_card(base_url=str(request.base_url), gateway=get_agent_gateway())


@router.post("/a2a/rpc", response_model=A2AJsonRpcResponse)
def a2a_json_rpc(payload: Any = Body(...)) -> A2AJsonRpcResponse:
    """Handle A2A JSON-RPC requests over the local gateway."""

    if not isinstance(payload, dict):
        return _error_response(
            None,
            JSONRPC_INVALID_REQUEST,
            "JSON-RPC request must be an object.",
        )
    try:
        request = A2AJsonRpcRequest.model_validate(payload)
    except Exception as exc:
        return _error_response(
            payload.get("id") if isinstance(payload, dict) else None,
            JSONRPC_INVALID_REQUEST,
            "Invalid JSON-RPC request.",
            data={"detail": str(exc)},
        )
    if request.method not in A2A_SEND_MESSAGE_METHODS:
        return _error_response(
            request.id,
            JSONRPC_METHOD_NOT_FOUND,
            f"Unsupported A2A method: {request.method}",
            data={"method": request.method},
        )
    try:
        gateway_request = gateway_request_from_a2a_params(request.params)
    except A2AInvalidParams as exc:
        return _error_response(
            request.id,
            JSONRPC_INVALID_PARAMS,
            str(exc),
        )
    access = get_trial_access_gate().check(gateway_request.user_id)
    if not access.allowed:
        return _error_response(
            request.id,
            JSONRPC_INVALID_PARAMS,
            access.reason or "trial user is not allowed",
            data={"user_id": gateway_request.user_id},
        )
    try:
        response = get_agent_gateway().run(gateway_request)
    except Exception as exc:  # pragma: no cover - defensive protocol boundary
        return _error_response(
            request.id,
            JSONRPC_INTERNAL_ERROR,
            "A2A request failed.",
            data={"detail": str(exc)},
        )
    source_message = request.params.get("message") if isinstance(request.params.get("message"), dict) else {}
    return A2AJsonRpcResponse(
        jsonrpc=A2A_JSONRPC_VERSION,
        id=request.id,
        result=task_from_gateway_response(
            response,
            request=gateway_request,
            source_message=source_message,
        ),
    )


def _error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> A2AJsonRpcResponse:
    return A2AJsonRpcResponse(
        jsonrpc=A2A_JSONRPC_VERSION,
        id=request_id,
        error={
            "code": code,
            "message": message,
            "data": data or {},
        },
    )
