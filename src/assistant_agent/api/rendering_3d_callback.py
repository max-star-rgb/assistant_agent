"""Acknowledge image-to-3D completion notifications."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, HttpUrl


class Rendering3DCallback(BaseModel):
    mediaType: str = Field(min_length=1)
    mediaUrl: HttpUrl
    image: str | None = None


def create_rendering_3d_callback_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/calling-agent-service/v1/{session_id}/{chat_index}/3d-gen-back"
    )
    async def rendering_3d_callback(
        session_id: str,
        chat_index: str,
        callback: Rendering3DCallback,
    ) -> dict:
        _ = (session_id, chat_index, callback)
        return {
            "errCode": 0,
            "errMessage": "success",
            "data": {"result": "SUCCESS"},
        }

    return router


router = create_rendering_3d_callback_router()
