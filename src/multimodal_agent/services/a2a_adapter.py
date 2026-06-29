"""Inbound A2A JSON-RPC adapter over the local AgentGateway."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from multimodal_agent.schemas.a2a import (
    A2A_PROTOCOL_VERSION,
    A2AArtifact,
    A2AMessage,
    A2ATaskResult,
    A2ATaskStatus,
)
from multimodal_agent.schemas.agent_gateway import AgentCollaborationMode, AgentGatewayRunRequest
from multimodal_agent.schemas.api import AgentRunResponse
from multimodal_agent.services.agent_gateway import AgentGateway


DEFAULT_A2A_USER_ID = "a2a_user"
DEFAULT_A2A_SESSION_ID = "a2a_session"


class A2AInvalidParams(ValueError):
    """Raised when an inbound A2A message cannot be mapped safely."""


def build_agent_card(*, base_url: str, gateway: AgentGateway | None = None) -> dict[str, Any]:
    """Build a local A2A agent card for discovery."""

    normalized_base = base_url.rstrip("/")
    skills = _skills_from_gateway(gateway)
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "Multimodal Agent",
        "description": "Local-first multimodal assistant with explicit local multi-agent gateway routing.",
        "url": f"{normalized_base}/a2a/rpc",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [
            {
                "transport": "JSONRPC",
                "url": f"{normalized_base}/a2a/rpc",
            }
        ],
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": skills,
        "supportsAuthenticatedExtendedCard": False,
    }


def gateway_request_from_a2a_params(params: Mapping[str, Any]) -> AgentGatewayRunRequest:
    """Convert A2A SendMessage params into an internal gateway request."""

    message = _mapping(params.get("message"), field_name="message")
    metadata = _merged_metadata(params, message)
    text, image_ids, video_ids, audio_id = _extract_parts(message)
    if not text and not image_ids and not video_ids and not audio_id:
        raise A2AInvalidParams("A2A message must include text or media parts.")
    session_id = (
        _metadata_string(metadata, "session_id", "sessionId")
        or _string(params.get("contextId"))
        or _string(message.get("contextId"))
        or DEFAULT_A2A_SESSION_ID
    )
    user_id = (
        _metadata_string(metadata, "user_id", "userId")
        or DEFAULT_A2A_USER_ID
    )
    collaboration_mode = _collaboration_mode(
        _metadata_string(metadata, "collaboration_mode", "collaborationMode", "mode")
    )
    return AgentGatewayRunRequest(
        user_id=user_id,
        session_id=session_id,
        text=text or None,
        image_ids=image_ids,
        video_ids=video_ids,
        audio_id=audio_id,
        target_agent_id=_metadata_string(metadata, "target_agent_id", "targetAgentId"),
        capability=_metadata_string(metadata, "capability"),
        collaboration_mode=collaboration_mode,
        metadata={
            "source": "a2a_json_rpc",
            "a2a": {
                "message_id": _string(message.get("messageId")),
                "context_id": _string(message.get("contextId")) or session_id,
                "task_id": _string(message.get("taskId")),
                "metadata": metadata,
            },
        },
    )


def task_from_gateway_response(
    response: AgentRunResponse,
    *,
    request: AgentGatewayRunRequest,
    source_message: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert an internal gateway response into an A2A task-like result."""

    context_id = _string(source_message.get("contextId")) or request.session_id
    source_message_id = _string(source_message.get("messageId")) or f"msg_{uuid4().hex}"
    reply_message = _agent_message(
        text=response.response_text,
        context_id=context_id,
        task_id=response.run_id,
    )
    state = "failed" if response.status == "failed" else "completed"
    task = A2ATaskResult(
        id=response.run_id,
        contextId=context_id,
        status=A2ATaskStatus(
            state=state,
            message=reply_message,
        ),
        artifacts=[
            A2AArtifact(
                artifactId=f"artifact_{response.run_id}",
                name="agent-response",
                parts=[{"kind": "text", "text": response.response_text}],
            )
        ],
        history=[
            _public_source_message(source_message, fallback_message_id=source_message_id, context_id=context_id),
            reply_message,
        ],
        metadata={
            "trace_id": response.trace_id,
            "runtime_status": response.status,
            "agent_gateway": response.data.get("agent_gateway", {}),
            "errors": [error.model_dump(mode="json") for error in response.errors],
        },
    )
    return task.model_dump(mode="json")


def _skills_from_gateway(gateway: AgentGateway | None) -> list[dict[str, Any]]:
    if gateway is None:
        return [
            {
                "id": "agent.default",
                "name": "Default Agent",
                "description": "Default local agent runtime.",
                "tags": ["local", "chat", "tool_calling"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain", "application/json"],
            }
        ]
    skills = []
    for instance in gateway.directory.list():
        skills.append(
            {
                "id": instance.agent_id,
                "name": instance.display_name,
                "description": instance.description,
                "tags": sorted(set(["local", *instance.capabilities])),
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain", "application/json"],
            }
        )
    return skills


def _merged_metadata(params: Mapping[str, Any], message: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source in (
        params.get("metadata"),
        _mapping_or_empty(params.get("configuration")).get("metadata"),
        message.get("metadata"),
    ):
        if isinstance(source, Mapping):
            metadata.update({str(key): value for key, value in source.items()})
    return metadata


def _extract_parts(message: Mapping[str, Any]) -> tuple[str, list[str], list[str], str | None]:
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise A2AInvalidParams("A2A message.parts must be a list.")
    text_parts: list[str] = []
    image_ids: list[str] = []
    video_ids: list[str] = []
    audio_id: str | None = None
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        kind = _string(part.get("kind") or part.get("type")).lower()
        if kind in {"text", "text/plain"} or "text" in part:
            text = _string(part.get("text"))
            if text:
                text_parts.append(text)
            continue
        file_ref = _file_ref(part)
        if not file_ref:
            continue
        mime_type = _string(part.get("mimeType") or part.get("mime_type") or _mapping_or_empty(part.get("file")).get("mimeType"))
        if mime_type.startswith("image/"):
            image_ids.append(file_ref)
        elif mime_type.startswith("video/"):
            video_ids.append(file_ref)
        elif mime_type.startswith("audio/") and audio_id is None:
            audio_id = file_ref
    return "\n".join(text_parts).strip(), image_ids, video_ids, audio_id


def _file_ref(part: Mapping[str, Any]) -> str:
    file_payload = _mapping_or_empty(part.get("file"))
    value = (
        part.get("uri")
        or part.get("url")
        or part.get("name")
        or file_payload.get("uri")
        or file_payload.get("url")
        or file_payload.get("name")
    )
    return _string(value)


def _agent_message(*, text: str, context_id: str, task_id: str) -> A2AMessage:
    return A2AMessage(
        role="agent",
        messageId=f"msg_{uuid4().hex}",
        contextId=context_id,
        taskId=task_id,
        parts=[{"kind": "text", "text": text}],
    )


def _public_source_message(
    message: Mapping[str, Any],
    *,
    fallback_message_id: str,
    context_id: str,
) -> A2AMessage:
    return A2AMessage(
        role=_string(message.get("role")) or "user",
        messageId=_string(message.get("messageId")) or fallback_message_id,
        contextId=_string(message.get("contextId")) or context_id,
        parts=list(message.get("parts") if isinstance(message.get("parts"), list) else []),
    )


def _collaboration_mode(value: str) -> AgentCollaborationMode:
    if not value:
        return "single"
    if value not in {"single", "controller_delegate"}:
        raise A2AInvalidParams(f"Unsupported collaboration mode: {value}")
    return value


def _metadata_string(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _string(metadata.get(key))
        if value:
            return value
    return None


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise A2AInvalidParams(f"A2A params.{field_name} must be an object.")
    return value


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""
