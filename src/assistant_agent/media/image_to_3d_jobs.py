"""Media-owned image-to-3D job and completion contracts."""

from __future__ import annotations

from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.identifiers import new_prefixed_uuid7


ImageTo3DJobStatus = Literal["generating", "completed", "failed"]


class ImageTo3DArtifact(BaseModel):
    """Neutral completion payload returned by the 3D service."""

    model_config = ConfigDict(frozen=True)

    media_type: str = Field(min_length=1)
    media_url: str | None = None
    image: str | None = None


class ImageTo3DJob(BaseModel):
    """One process-local asynchronous image-to-3D submission."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_image_id: str = Field(min_length=1)
    status: ImageTo3DJobStatus = "generating"
    artifact: ImageTo3DArtifact | None = None
    error: str | None = None


class ImageTo3DJobRegistry:
    """Thread-safe process-local store for asynchronous 3D job state."""

    def __init__(self) -> None:
        self._jobs: dict[str, ImageTo3DJob] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        user_id: str,
        session_id: str,
        source_image_id: str,
    ) -> ImageTo3DJob:
        job = ImageTo3DJob(
            job_id=new_prefixed_uuid7("image-to-3d", separator="-"),
            user_id=user_id,
            session_id=session_id,
            source_image_id=source_image_id,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> ImageTo3DJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_for_owner(
        self,
        job_id: str,
        *,
        user_id: str,
        session_id: str,
    ) -> ImageTo3DJob | None:
        job = self.get(job_id)
        if job is None or job.user_id != user_id or job.session_id != session_id:
            return None
        return job

    def complete(
        self,
        job_id: str,
        *,
        artifact: ImageTo3DArtifact,
    ) -> ImageTo3DJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            completed = job.model_copy(
                update={
                    "status": "completed",
                    "artifact": artifact,
                    "error": None,
                }
            )
            self._jobs[job_id] = completed
            return completed

    def fail(self, job_id: str, *, error: str) -> ImageTo3DJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            failed = job.model_copy(
                update={
                    "status": "failed",
                    "error": error,
                }
            )
            self._jobs[job_id] = failed
            return failed


_image_to_3d_job_registry = ImageTo3DJobRegistry()


def get_image_to_3d_job_registry() -> ImageTo3DJobRegistry:
    return _image_to_3d_job_registry
