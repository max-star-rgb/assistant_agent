"""Relay image-to-3D completion notifications to the active media socket."""

from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl

from assistant_agent.media.rendering_3d_relay import (
    Rendering3DRelayBinding,
    Rendering3DRelayRegistry,
    Rendering3DRelayUnavailable,
    get_rendering_3d_relay_registry,
)
from assistant_agent.observability.operational_logging import digest_identifier


logger = logging.getLogger("assistant_agent.api.rendering_3d_callback")


class Rendering3DCallback(BaseModel):
    mediaType: Literal["ply", "glb", "mp4"]
    mediaUrl: HttpUrl
    image: str | None = None


def create_rendering_3d_callback_router(
    relay_registry: Rendering3DRelayRegistry | None = None,
) -> APIRouter:
    router = APIRouter()
    registry = relay_registry or get_rendering_3d_relay_registry()

    @router.post(
        "/calling-agent-service/v1/{session_id}/{chat_index}/3d-gen-back"
    )
    async def rendering_3d_callback(
        session_id: str,
        chat_index: str,
        callback: Rendering3DCallback,
    ) -> dict:
        try:
            await registry.send(
                session_id,
                lambda binding: _chat_response(
                    binding=binding,
                    chat_index=chat_index,
                    callback=callback,
                ),
            )
        except Rendering3DRelayUnavailable as exc:
            logger.warning(
                "3d callback relay unavailable session_digest=%s media_type=%s",
                digest_identifier(session_id),
                callback.mediaType,
            )
            raise HTTPException(
                status_code=409,
                detail={"errCode": 1, "errMessage": "media session unavailable"},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - HTTP/WebSocket delivery boundary.
            logger.warning(
                "3d callback relay failed session_digest=%s media_type=%s",
                digest_identifier(session_id),
                callback.mediaType,
            )
            raise HTTPException(
                status_code=503,
                detail={"errCode": 1, "errMessage": "media delivery failed"},
            ) from exc
        return {
            "errCode": 0,
            "errMessage": "success",
            "data": {"result": "SUCCESS"},
        }

    return router


def _chat_response(
    *,
    binding: Rendering3DRelayBinding,
    chat_index: str,
    callback: Rendering3DCallback,
) -> dict[str, str]:
    media_url = str(callback.mediaUrl)
    detail = (
        {"type": "VIDEO", "videoUrl": media_url}
        if callback.mediaType == "mp4"
        else {"type": "TD_MODEL", "modelUrl": media_url}
    )
    body = {
        "chatIndex": chat_index,
        "number": binding.number,
        "messageType": "ANSWER",
        "display_only": False,
        "message": {
            "type": "BRIEF",
            "chatIndex": chat_index,
            "content": {
                "intentExecution": {
                    "description": "",
                    "plans": [],
                    "messageType": "ANSWER",
                },
                "intentResult": {
                    "description": "小艺已经为您生成3D蛋糕模型",
                    "status": "SUCCESS",
                    "plan": [],
                    "messageType": "ANSWER",
                    "detail": [detail],
                },
                "intentWeb": {
                    "description": "",
                    "resourceType": "",
                    "resourceUrl": "",
                },
            },
        },
    }
    return {
        "message": "chatResponse",
        "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    }


router = create_rendering_3d_callback_router()
