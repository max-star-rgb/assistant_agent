"""Project 3D callbacks onto the active Media-Service relay socket."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, HttpUrl

from assistant_agent.media.media_relay_delivery import (
    MediaRelayConnectionRegistry,
    media_relay_connection_registry,
)


class Rendering3DCallback(BaseModel):
    mediaType: Literal["ply", "glb", "mp4"]
    mediaUrl: HttpUrl
    image: str | None = None


def create_rendering_3d_callback_router(
    registry: MediaRelayConnectionRegistry = media_relay_connection_registry,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/calling-agent-service/v1/{session_id}/{chat_index}/3d-gen-back"
    )
    async def rendering_3d_callback(
        session_id: str,
        chat_index: str,
        callback: Rendering3DCallback,
    ) -> dict:
        delivered = await registry.deliver_3d_result(
            session_id=session_id,
            chat_index=chat_index,
            media_type=callback.mediaType,
            model_url=str(callback.mediaUrl),
        )
        if not delivered:
            return {
                "errCode": 0,
                "errMessage": "failed",
                "data": {"result": "未找到可用的媒体中继连接"},
            }
        return {
            "errCode": 0,
            "errMessage": "success",
            "data": {"result": "SUCCESS"},
        }

    return router


router = create_rendering_3d_callback_router()
