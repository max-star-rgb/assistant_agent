"""Thin HTTP client for the external visual Memory Service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class MemoryServiceRequest:
    method: str
    path: str
    body: Mapping[str, Any]


@dataclass(frozen=True)
class MemoryMediaFile:
    file_id: str
    file_url: str
    filename: str
    media_type: str
    start_time: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class MemoryUploadResult:
    task_id: str
    status: str
    accepted_count: int


@dataclass(frozen=True)
class MemoryTaskStatus:
    task_id: str
    status: str


MemoryServiceTransport = Callable[[MemoryServiceRequest], Awaitable[Mapping[str, Any]]]


class RemoteMemoryServiceClient:
    """Expose only the external endpoints consumed by assistant_agent."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        transport: MemoryServiceTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._send

    async def query_memories(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int,
    ) -> tuple[str, ...]:
        response = await self._transport(
            MemoryServiceRequest(
                method="POST",
                path="/v1/memories/query",
                body={
                    "user_id": user_id,
                    "query": query,
                    "top_k": top_k,
                    "direct_answer": False,
                },
            )
        )
        results = response.get("results")
        if not isinstance(results, list):
            raise ValueError("Memory Service query returned invalid results")
        texts: list[str] = []
        for item in results:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            if len(texts) >= top_k:
                break
        return tuple(texts)

    async def upload_media(
        self,
        *,
        user_id: str,
        session_id: str,
        files: tuple[MemoryMediaFile, ...],
    ) -> MemoryUploadResult:
        if not files:
            raise ValueError("Memory Service upload requires at least one file")
        response = await self._transport(
            MemoryServiceRequest(
                method="POST",
                path="/v1/media/upload",
                body={
                    "user_id": user_id,
                    "session_id": session_id,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "file_url": item.file_url,
                            "filename": item.filename,
                            "media_type": item.media_type,
                            "start_time": item.start_time,
                            "metadata": dict(item.metadata),
                        }
                        for item in files
                    ],
                },
            )
        )
        return MemoryUploadResult(
            task_id=str(response.get("task_id") or ""),
            status=str(response.get("status") or "unknown"),
            accepted_count=_safe_int(response.get("accepted_count")),
        )

    async def task_status(
        self,
        *,
        user_id: str,
        task_id: str,
    ) -> MemoryTaskStatus:
        response = await self._transport(
            MemoryServiceRequest(
                method="POST",
                path="/v1/tasks_status",
                body={"user_id": user_id, "task_id": task_id},
            )
        )
        return MemoryTaskStatus(
            task_id=str(response.get("task_id") or task_id),
            status=str(response.get("status") or "unknown"),
        )

    async def _send(self, request: MemoryServiceRequest) -> Mapping[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.request(
                request.method,
                request.path,
                json=dict(request.body),
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Memory Service returned a non-object response")
        return payload


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "MemoryServiceRequest",
    "MemoryServiceTransport",
    "MemoryMediaFile",
    "MemoryTaskStatus",
    "MemoryUploadResult",
    "RemoteMemoryServiceClient",
]
