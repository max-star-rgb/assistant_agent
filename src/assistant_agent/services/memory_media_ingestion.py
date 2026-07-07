"""Governed service boundary for external Memory Server media ingestion."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.remote import MemoryServerMediaFile, RemoteMemoryClient
from assistant_agent.schemas.identity import RequestIdentity


FileIdFactory = Callable[[RequestIdentity, "MemoryMediaIngestionFile", int], str]
_PROVIDER_UNCONFIGURED_ERROR = {
    "code": "provider_unconfigured",
    "message": "Memory Server media ingestion is not configured.",
    "recoverable": True,
}


class MemoryMediaIngestionFile(BaseModel):
    """Tool/service-facing safe media file reference."""

    file_id: str | None = None
    file_url: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    start_time: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryMediaIngestionResult(BaseModel):
    """Structured media ingestion result for tools and API wrappers."""

    status: str
    task_id: str = ""
    accepted_count: int = Field(default=0, ge=0)
    file_ids: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    output_ref: str | None = None


class MemoryMediaTaskStatusResult(BaseModel):
    """Structured task status result for tools and API wrappers."""

    task_id: str = ""
    status: str
    total_files: int = Field(default=0, ge=0)
    processed_files: int = Field(default=0, ge=0)
    failed_files: int = Field(default=0, ge=0)
    estimated_completion_seconds: float | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    code: int = 0
    scope_warning: str | None = None
    output_ref: str | None = None


class MemoryMediaIngestionService:
    """Bind runtime identity before calling the external Memory Server client."""

    def __init__(
        self,
        *,
        remote_client: RemoteMemoryClient | None,
        file_id_factory: FileIdFactory | None = None,
    ) -> None:
        self.remote_client = remote_client
        self.file_id_factory = file_id_factory or _default_file_id

    def ingest(
        self,
        *,
        identity: RequestIdentity,
        files: list[MemoryMediaIngestionFile],
    ) -> MemoryMediaIngestionResult:
        if self.remote_client is None:
            return MemoryMediaIngestionResult(
                status="provider_unconfigured",
                errors=[dict(_PROVIDER_UNCONFIGURED_ERROR)],
            )
        session_id = identity.session_id or "default"
        resolved_files = [
            _server_file_from_ingestion_file(
                file,
                file_id=file.file_id or self.file_id_factory(identity, file, index),
            )
            for index, file in enumerate(files)
        ]
        result = self.remote_client.upload_media(
            user_id=identity.user_id,
            session_id=session_id,
            files=resolved_files,
        )
        task_id = result.task_id
        return MemoryMediaIngestionResult(
            status=result.status,
            task_id=task_id,
            accepted_count=result.accepted_count,
            file_ids=[file.file_id for file in resolved_files],
            errors=result.errors,
            output_ref=_task_output_ref(task_id),
        )

    def task_status(
        self,
        *,
        identity: RequestIdentity,
        task_id: str,
    ) -> MemoryMediaTaskStatusResult:
        if self.remote_client is None:
            return MemoryMediaTaskStatusResult(
                task_id=task_id,
                status="provider_unconfigured",
                errors=[dict(_PROVIDER_UNCONFIGURED_ERROR)],
                output_ref=_task_output_ref(task_id),
            )
        result = self.remote_client.task_status(user_id=identity.user_id, task_id=task_id)
        return MemoryMediaTaskStatusResult(
            task_id=result.task_id,
            status=result.status,
            total_files=result.total_files,
            processed_files=result.processed_files,
            failed_files=result.failed_files,
            estimated_completion_seconds=result.estimated_completion_seconds,
            statistics=result.statistics,
            results=result.results,
            errors=result.errors,
            code=result.code,
            scope_warning=result.scope_warning,
            output_ref=_task_output_ref(result.task_id or task_id),
        )


def create_memory_media_ingestion_service(config: ProviderConfig | None = None) -> MemoryMediaIngestionService:
    """Create the media-ingestion service without enabling remote calls by default."""

    resolved_config = config or ProviderConfig.from_env()
    if resolved_config.memory_backend != "hybrid_remote" or not resolved_config.memory_server_base_url:
        return MemoryMediaIngestionService(remote_client=None)
    return MemoryMediaIngestionService(
        remote_client=RemoteMemoryClient(
            base_url=resolved_config.memory_server_base_url,
            timeout_seconds=resolved_config.memory_server_timeout_seconds,
            query_strategy=resolved_config.memory_server_query_strategy,
            include_media_chunks=resolved_config.memory_server_include_media_chunks,
            direct_answer=resolved_config.memory_server_direct_answer,
        )
    )


def _server_file_from_ingestion_file(file: MemoryMediaIngestionFile, *, file_id: str) -> MemoryServerMediaFile:
    return MemoryServerMediaFile(
        file_id=file_id,
        file_url=file.file_url,
        filename=file.filename,
        media_type=file.media_type,
        start_time=file.start_time,
        metadata=file.metadata,
    )


def _task_output_ref(task_id: str) -> str | None:
    return f"memory_server://tasks/{task_id}" if task_id else None


def _default_file_id(identity: RequestIdentity, file: MemoryMediaIngestionFile, index: int) -> str:
    prefix = "-".join(
        value
        for value in (
            "assistant-agent",
            _safe_identifier(identity.user_id),
            _safe_identifier(identity.session_id or "default"),
            _safe_identifier(file.media_type),
            str(index),
        )
        if value
    )
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"
