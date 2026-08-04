"""Session visual-history retrieval with bounded VLM verification."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.consumers.temporal_memory import TemporalVisualMemory
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingFailureEvent, TextObservation
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)


VisualMemorySearchStatus = Literal[
    "confirmed", "candidate", "uncertain", "not_found", "unavailable"
]
VerificationStatus = Literal["skipped", "succeeded", "failed"]


class VisionUnderstandingClient(Protocol):
    def understand(self, request: VisionUnderstandingRequest) -> VisionUnderstandingResult: ...


class VisualMemorySearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=4_000)
    top_k: int = Field(default=5, ge=1, le=20)
    verification_top_k: int = Field(default=3, ge=1, le=8)
    as_of_sequence: int | None = Field(default=None, ge=0)
    since_ms: int | None = Field(default=None, ge=0)
    until_ms: int | None = Field(default=None, ge=0)


class VisualMemoryMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_observation_id: str
    video_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)
    similarity: float = Field(ge=-1.0, le=1.0)
    evidence_ref: str = Field(exclude=True)


class VisualMemorySearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VisualMemorySearchStatus
    verification_status: VerificationStatus = "skipped"
    matches: list[VisualMemoryMatch] = Field(default_factory=list)
    errors: list[dict[str, object]] = Field(default_factory=list)


class VisualMemorySearchService:
    """Recall by text embedding, then verify only bounded owned evidence."""

    def __init__(
        self,
        *,
        coordinator: SessionEmbeddingCoordinator,
        temporal_memory: TemporalVisualMemory,
        vision_client: VisionUnderstandingClient,
    ) -> None:
        self.coordinator = coordinator
        self.temporal_memory = temporal_memory
        self.vision_client = vision_client

    def search(self, request: VisualMemorySearchRequest) -> VisualMemorySearchResult:
        if request.session_id != self.coordinator.session_id:
            raise ValueError("visual_memory_search_session_mismatch")
        if not self.temporal_memory.has_history():
            return VisualMemorySearchResult(status="not_found")

        query = self.coordinator.embed_text(
            TextObservation(
                session_id=request.session_id,
                observation_id=request.request_id,
                text=request.query.strip(),
                source="visual_memory_search",
            ),
            priority="interactive",
        )
        if isinstance(query, EmbeddingFailureEvent):
            return VisualMemorySearchResult(
                status="unavailable",
                errors=[
                    {
                        "code": query.code,
                        "message": query.safe_message,
                        "recoverable": query.recoverable,
                    }
                ],
            )

        record_count = len(self.temporal_memory.records())
        candidates = self.temporal_memory.search_candidates(
            query,
            top_k=max(request.top_k, record_count),
            since_ms=request.since_ms,
            until_ms=request.until_ms,
        )
        if request.as_of_sequence is not None:
            candidates = [
                item
                for item in candidates
                if item.record.frame_sequence is not None
                and item.record.frame_sequence <= request.as_of_sequence
            ]
        candidates = candidates[: request.top_k]
        if not candidates:
            return VisualMemorySearchResult(status="not_found")
        matches = [
            VisualMemoryMatch(
                image_observation_id=item.record.source_observation_id,
                video_id=item.record.video_id,
                frame_sequence=item.record.frame_sequence,
                captured_at_ms=item.record.captured_at_ms,
                similarity=item.similarity,
                evidence_ref=item.record.evidence_ref,
            )
            for item in candidates
        ]
        verification_refs = [
            item.record.evidence_ref
            for item in candidates[: request.verification_top_k]
        ]
        try:
            verification = self.vision_client.understand(
                VisionUnderstandingRequest(
                    image_ids=verification_refs,
                    question=(
                        "请只判断这些历史画面是否包含用户寻找的物体，并描述可确认的画面。"
                    ),
                    user_query=request.query,
                    session_id=request.session_id,
                    max_frames=len(verification_refs),
                    sample_strategy="provided_candidates_only",
                    metadata={"capability": "visual_memory_search_verification"},
                )
            )
        except Exception:
            return VisualMemorySearchResult(
                status="candidate",
                verification_status="failed",
                matches=matches,
                errors=[
                    {
                        "code": "visual_verification_failed",
                        "message": "visual verification failed",
                        "recoverable": True,
                    }
                ],
            )
        if verification.errors:
            return VisualMemorySearchResult(
                status="candidate",
                verification_status="failed",
                matches=matches,
                errors=[
                    {
                        "code": "visual_verification_failed",
                        "message": "visual verification failed",
                        "recoverable": True,
                    }
                ],
            )
        confirmed = _verification_mentions_query(request.query, verification)
        return VisualMemorySearchResult(
            status="confirmed" if confirmed else "uncertain",
            verification_status="succeeded",
            matches=matches,
        )


def _verification_mentions_query(
    query: str,
    result: VisionUnderstandingResult,
) -> bool:
    normalized_query = "".join(query.casefold().split())
    if not normalized_query:
        return False
    evidence = [
        result.summary,
        *result.objects,
        *result.products,
        *result.text_in_media,
        *result.text_in_video,
    ]
    normalized_evidence = "".join("".join(value.casefold().split()) for value in evidence)
    return normalized_query in normalized_evidence
