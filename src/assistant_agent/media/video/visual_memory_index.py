"""Backend-neutral contracts for timestamped VLM-text retrieval."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class VisualMemoryIndexError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)
    recoverable: bool = True


class VisualMemoryIndexDocument(BaseModel):
    """One complete single-frame VLM text and its trusted identity metadata."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1, max_length=160)
    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    video_id: str = Field(min_length=1, max_length=240)
    frame_sequence: int = Field(ge=0)
    captured_at_ms: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4_000)


class VisualMemoryIndexQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1, max_length=240)
    session_id: str = Field(min_length=1, max_length=240)
    query: str = Field(min_length=1, max_length=4_000)
    as_of_sequence: int | None = Field(default=None, ge=0)
    since_ms: int | None = Field(default=None, ge=0)
    until_ms: int | None = Field(default=None, ge=0)
    freshness_record_id: str | None = Field(default=None, min_length=1, max_length=160)
    limit: int = Field(default=12, ge=1, le=100)


class VisualMemoryIndexHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: VisualMemoryIndexDocument
    score: float


class VisualMemoryIndexWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "unavailable"]
    errors: list[VisualMemoryIndexError] = Field(default_factory=list)


class VisualMemoryIndexSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["records", "empty", "unavailable"]
    hits: list[VisualMemoryIndexHit] = Field(default_factory=list)
    coverage_complete: bool = True
    errors: list[VisualMemoryIndexError] = Field(default_factory=list)


class VisualMemoryTextIndex(Protocol):
    def upsert(
        self,
        document: VisualMemoryIndexDocument,
    ) -> VisualMemoryIndexWriteResult: ...

    def search(
        self,
        query: VisualMemoryIndexQuery,
    ) -> VisualMemoryIndexSearchResult: ...

    def delete_session(self, user_id: str, session_id: str) -> None: ...

    def delete_user(self, user_id: str) -> None: ...

    def close(self) -> None: ...


class UnavailableVisualMemoryTextIndex:
    """Explicit degraded backend; never performs a semantic fallback."""

    def __init__(self, *, code: str, message: str) -> None:
        self._error = VisualMemoryIndexError(code=code, message=message)

    def upsert(
        self,
        document: VisualMemoryIndexDocument,
    ) -> VisualMemoryIndexWriteResult:
        del document
        return VisualMemoryIndexWriteResult(
            status="unavailable",
            errors=[self._error],
        )

    def search(
        self,
        query: VisualMemoryIndexQuery,
    ) -> VisualMemoryIndexSearchResult:
        del query
        return VisualMemoryIndexSearchResult(
            status="unavailable",
            coverage_complete=False,
            errors=[self._error],
        )

    def delete_session(self, user_id: str, session_id: str) -> None:
        del user_id, session_id

    def delete_user(self, user_id: str) -> None:
        del user_id

    def close(self) -> None:
        return None
