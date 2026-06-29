"""Inbound A2A-compatible HTTP routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from multimodal_agent.api.auth import get_auth_context
from multimodal_agent.api.routes_agent import get_agent_gateway, get_trial_access_gate
from multimodal_agent.schemas.a2a import (
    A2AAgentCard,
    A2A_JSONRPC_VERSION,
    A2A_SEND_MESSAGE_METHODS,
    A2AJsonRpcRequest,
    A2AJsonRpcResponse,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
)
from multimodal_agent.services.a2a_adapter import (
    A2AInvalidParams,
    build_agent_card,
    gateway_request_from_a2a_params,
    task_from_gateway_response,
)
from multimodal_agent.services.api_identity import AuthContext, resolve_request_identity


router = APIRouter()


@router.get("/.well-known/agent-card.json", response_model=A2AAgentCard)
def get_agent_card(request: Request) -> dict[str, Any]:
    """Expose a local A2A agent card for discovery."""

    return build_agent_card(base_url=str(request.base_url), gateway=get_agent_gateway())


@router.post("/a2a/rpc", response_model=A2AJsonRpcResponse)
async def a2a_json_rpc(
    request: Request,
    auth_context: AuthContext = Depends(get_auth_context),
) -> A2AJsonRpcResponse:
    """Handle A2A JSON-RPC requests over the local gateway."""

    payload = await _read_json_payload(request)
    if isinstance(payload, _JsonParseFailure):
        return _error_response(
            None,
            JSONRPC_PARSE_ERROR,
            "Parse error.",
            data={"detail": payload.message},
        )
    if not isinstance(payload, dict):
        return _error_response(
            None,
            JSONRPC_INVALID_REQUEST,
            "JSON-RPC request must be an object.",
        )
    try:
        rpc_request = A2AJsonRpcRequest.model_validate(payload)
    except Exception as exc:
        return _error_response(
            _request_id_from_payload(payload),
            JSONRPC_INVALID_REQUEST,
            "Invalid JSON-RPC request.",
            data={"detail": _request_validation_detail(exc)},
        )
    if rpc_request.params is None:
        rpc_request.params = {}
    if not isinstance(rpc_request.params, dict):
        return _error_response(
            rpc_request.id,
            JSONRPC_INVALID_PARAMS,
            "A2A params must be an object.",
        )
    if rpc_request.method not in A2A_SEND_MESSAGE_METHODS:
        return _error_response(
            rpc_request.id,
            JSONRPC_METHOD_NOT_FOUND,
            f"Unsupported A2A method: {rpc_request.method}",
            data={"method": rpc_request.method},
        )
    try:
        gateway_request = gateway_request_from_a2a_params(rpc_request.params)
    except A2AInvalidParams as exc:
        return _error_response(
            rpc_request.id,
            JSONRPC_INVALID_PARAMS,
            str(exc),
        )
    try:
        identity = resolve_request_identity(
            user_id=gateway_request.user_id,
            session_id=gateway_request.session_id,
            source="a2a_metadata",
            auth_context=auth_context,
        )
    except ValueError as exc:
        return _error_response(
            rpc_request.id,
            JSONRPC_INVALID_PARAMS,
            str(exc),
        )
    access = identity.trial_access(get_trial_access_gate())
    if not access.allowed:
        return _error_response(
            rpc_request.id,
            JSONRPC_INVALID_PARAMS,
            access.reason or "trial user is not allowed",
            data={"user_id": identity.identity.user_id},
        )
    try:
        response = get_agent_gateway().run(gateway_request)
    except Exception as exc:  # pragma: no cover - defensive protocol boundary
        return _error_response(
            rpc_request.id,
            JSONRPC_INTERNAL_ERROR,
            "A2A request failed.",
            data={"detail": exc.__class__.__name__},
        )
    source_message = rpc_request.params.get("message") if isinstance(rpc_request.params.get("message"), dict) else {}
    return A2AJsonRpcResponse(
        jsonrpc=A2A_JSONRPC_VERSION,
        id=rpc_request.id,
        result=task_from_gateway_response(
            response,
            request=gateway_request,
            source_message=source_message,
        ),
    )


class _JsonParseFailure:
    def __init__(self, message: str) -> None:
        self.message = message


async def _read_json_payload(request: Request) -> Any | _JsonParseFailure:
    raw = await request.body()
    if not raw:
        return _JsonParseFailure("Request body is empty.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return _JsonParseFailure(exc.msg)
    except UnicodeDecodeError:
        return _JsonParseFailure("Request body is not valid UTF-8 JSON.")


def _request_validation_detail(exc: Exception) -> str:
    if hasattr(exc, "errors"):
        return "schema_validation"
    return exc.__class__.__name__


def _request_id_from_payload(payload: Any) -> str | int | None:
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("id")
    if isinstance(request_id, bool):
        return None
    return request_id if isinstance(request_id, str | int) or request_id is None else None


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
