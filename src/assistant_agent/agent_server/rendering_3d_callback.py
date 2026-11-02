"""Receive image-to-3D completions through the Agent Server media adapter."""

from __future__ import annotations

import logging
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter
from pydantic import BaseModel

from assistant_agent.media.artifact_delivery import (
    ArtifactCompleted,
    MediaArtifactDeliveryHub,
    get_media_artifact_delivery_hub,
)
from assistant_agent.media.image_to_3d_jobs import (
    ImageTo3DArtifact,
    ImageTo3DJobRegistry,
    get_image_to_3d_job_registry,
)


DELIVERABLE_MEDIA_TYPES = frozenset({"mp4", "glb", "ply", "image"})
logger = logging.getLogger(__name__)


class Rendering3DCallback(BaseModel):
    mediaType: str
    mediaUrl: str | None = None
    image: str | None = None


class ArtifactDeliveryUnavailable(RuntimeError):
    """Legacy callback could not be associated with an active subscriber."""


def create_rendering_3d_callback_router(
    delivery_hub: MediaArtifactDeliveryHub | None = None,
    *,
    job_registry: ImageTo3DJobRegistry | None = None,
) -> APIRouter:
    router = APIRouter()
    hub = delivery_hub or get_media_artifact_delivery_hub()
    jobs = job_registry or get_image_to_3d_job_registry()

    @router.post(
        "/calling-agent-service/v1/{job_or_session_id}/{chat_index}/3d-gen-back"
    )
    async def rendering_3d_callback(
        job_or_session_id: str,
        chat_index: str,
        callback: Rendering3DCallback,
    ) -> dict[str, str]:
        _ = chat_index
        correlation = str(uuid5(NAMESPACE_URL, f"artifact:{job_or_session_id}"))[:12]
        logger.info(
            "image_to_3d_callback_received chat=%s media_type=%s url_present=%s",
            correlation,
            callback.mediaType if callback.mediaType in DELIVERABLE_MEDIA_TYPES else "unsupported",
            bool(callback.mediaUrl),
        )
        job = jobs.complete(
            job_or_session_id,
            artifact=ImageTo3DArtifact(
                media_type=callback.mediaType,
                media_url=callback.mediaUrl,
                image=callback.image,
            ),
        )
        if callback.mediaType not in DELIVERABLE_MEDIA_TYPES:
            return {"code": "success"}

        event = ArtifactCompleted(
            artifact_id=job.job_id if job is not None else job_or_session_id,
            user_id=job.user_id if job is not None else None,
            session_id=job.session_id if job is not None else job_or_session_id,
            media_type=callback.mediaType,
            uri=callback.mediaUrl,
            inline_data=callback.image,
        )
        delivered = await hub.publish(event)
        logger.info(
            "image_to_3d_callback_dispatched chat=%s job_found=%s sender_completed=%s",
            correlation, job is not None, delivered,
        )
        if job is None and not delivered:
            # 老请求没有 job 可供轮询，必须让上游重试，不能虚假确认成功。
            raise ArtifactDeliveryUnavailable(event.session_id)
        return {"code": "success"}

    return router


router = create_rendering_3d_callback_router()
