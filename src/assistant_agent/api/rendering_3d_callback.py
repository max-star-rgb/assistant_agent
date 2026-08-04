"""Relay image-to-3D completion notifications to the active media socket."""

from __future__ import annotations

import json
import random
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel

from assistant_agent.media.image_to_3d_completion import (
    ImageTo3DArtifact,
    ImageTo3DJobRegistry,
    get_image_to_3d_job_registry,
)
from assistant_agent.media.rendering_3d_relay import (
    Rendering3DRelayBinding,
    Rendering3DRelayRegistry,
    get_rendering_3d_relay_registry,
)


TD_GEN_CALLBACK_RESPONSES = {
    "mp4": {
        "zh": ["已为您生成预览视频，请查看", "预览视频已生成，请查看"],
        "en": ["I've generated a preview video for you, please check out"],
    },
    "glb": {
        "zh": ["已为您生成3d模型，请查看", "3d模型已生成，请查看"],
        "en": ["I've generated a 3d model for you, please check out"],
    },
    "ply": {
        "zh": ["已为您生成3d模型，请查看", "3d模型已生成，请查看"],
        "en": ["I've generated a 3d model for you, please check out"],
    },
    "image": {
        "zh": ["已为您生成图片，请查看", "图片已生成，请查看"],
        "en": ["I've generated an image for you, please check out"],
    },
}


class Rendering3DCallback(BaseModel):
    mediaType: str
    mediaUrl: str | None = None
    image: str | None = None


def create_rendering_3d_callback_router(
    relay_registry: Rendering3DRelayRegistry | None = None,
    *,
    job_registry: ImageTo3DJobRegistry | None = None,
) -> APIRouter:
    router = APIRouter()
    registry = relay_registry or get_rendering_3d_relay_registry()
    jobs = job_registry or get_image_to_3d_job_registry()

    @router.post(
        "/calling-agent-service/v1/{session_id}/{chat_index}/3d-gen-back"
    )
    async def rendering_3d_callback(
        session_id: str,
        chat_index: str,
        callback: Rendering3DCallback,
    ) -> dict:
        _ = chat_index
        job = jobs.complete(
            session_id,
            artifact=ImageTo3DArtifact(
                media_type=callback.mediaType,
                media_url=callback.mediaUrl,
                image=callback.image,
            ),
        )
        if callback.mediaType not in TD_GEN_CALLBACK_RESPONSES:
            return {"code": "success"}
        if job is not None:
            if job.delivery_target != "agent_service":
                return {"code": "success"}
            delivery_session_id = job.session_id
        else:
            # Compatibility for submissions created before callback job IDs existed.
            delivery_session_id = session_id
        await registry.send(
            delivery_session_id,
            lambda binding: _chat_response(
                binding=binding,
                callback=callback,
            ),
        )
        return {"code": "success"}

    return router


def _chat_response(
    *,
    binding: Rendering3DRelayBinding,
    callback: Rendering3DCallback,
) -> dict[str, str]:
    if callback.mediaType in {"ply", "glb"}:
        detail = {"type": "TD_MODEL", "modelUrl": callback.mediaUrl}
    elif callback.mediaType == "mp4":
        detail = {"type": "VIDEO", "videoUrl": callback.mediaUrl}
    else:
        detail = {"type": "IMAGE", "image": callback.image}
    response = random.choice(
        TD_GEN_CALLBACK_RESPONSES[callback.mediaType][binding.language]
    )
    body = {
        "number": binding.number,
        "message": {
            "type": "BRIEF",
            "chatIndex": str(uuid4()),
            "content": {
                "intentExecution": {
                    "description": "",
                    "plans": [],
                    "messageType": "ANSWER",
                },
                "intentResult": {
                    "description": response,
                    "status": "SUCCESS",
                    "plan": [],
                    "messageType": "END",
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
