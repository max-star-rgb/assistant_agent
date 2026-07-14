"""Isolated HTTP adapters for Hindsight 0.8.4 and Mem0 OSS 2.0.11."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

from assistant_agent.memory.framework.base import FrameworkHttpRequest
from assistant_agent.memory.framework.http import FrameworkTransport, urllib_framework_transport
from assistant_agent.schemas.memory_framework import (
    FrameworkHealthResult,
    FrameworkMemoryRecord,
    FrameworkRecallRequest,
    FrameworkRecallResult,
    FrameworkRetainRequest,
    FrameworkRetainResult,
    MemoryEngineIdentity,
)


class _HttpEngineAdapter:
    def __init__(self, *, base_url: str, timeout_seconds: float, transport: FrameworkTransport | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or urllib_framework_transport(self.base_url)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        return self._transport(
            FrameworkHttpRequest(
                method=method,
                path=path,
                body=body,
                query=query,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
            )
        )


class UnavailableMemoryEngineAdapter:
    """Recoverable adapter used when framework mode lacks a sidecar URL."""

    def __init__(self, *, name: str) -> None:
        self.name = name

    def _raise(self, operation: str):
        from assistant_agent.memory.remote import MemoryServiceOperationError

        raise MemoryServiceOperationError(operation, "memory framework sidecar is not configured")

    def health(self):
        return FrameworkHealthResult(status="unavailable")

    def retain(self, request): return self._raise("retain")
    def recall(self, request): return self._raise("recall")
    def reflect(self, request): return self._raise("reflect")
    def get(self, **kwargs): return self._raise("get")
    def list(self, **kwargs): return self._raise("list")
    def history(self, **kwargs): return self._raise("history")
    def delete(self, **kwargs): return self._raise("delete")
    def clear(self, **kwargs): return self._raise("clear")
    def export(self, **kwargs): return self._raise("export")


class HindsightMemoryEngineAdapter(_HttpEngineAdapter):
    name = "hindsight"

    def __init__(self, *, base_url: str, timeout_seconds: float = 5.0, transport: FrameworkTransport | None = None) -> None:
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, transport=transport)

    def _bank_path(self, identity: MemoryEngineIdentity, suffix: str = "") -> str:
        return f"/v1/default/banks/{quote(identity.bank_id, safe='')}{suffix}"

    def health(self) -> FrameworkHealthResult:
        payload = self._request("GET", "/health")
        return FrameworkHealthResult(status="ok", version=_optional_string(payload.get("version")))

    def retain(self, request: FrameworkRetainRequest) -> FrameworkRetainResult:
        payload = self._request(
            "POST",
            self._bank_path(request.identity, "/memories"),
            body={
                "items": [
                    {
                        "content": request.text,
                        "context": f"{request.memory_type}:{request.source}",
                        "timestamp": request.created_at.isoformat(),
                        "document_id": request.project_memory_id,
                        "tags": request.identity.hindsight_tags,
                        "metadata": _safe_metadata(request.metadata, request),
                    }
                ],
                "async": False,
            },
        )
        engine_ids: list[str] = []
        if payload.get("success"):
            listed = self._request(
                "GET",
                self._bank_path(request.identity, "/memories/list"),
                query={"document_id": request.project_memory_id, "limit": "1000"},
            )
            engine_ids = [str(item["id"]) for item in _mapping_list(listed.get("items")) if item.get("id")]
        return FrameworkRetainResult(
            accepted=bool(payload.get("success")),
            engine_ids=engine_ids,
            operation_id=_optional_string(payload.get("operation_id")),
        )

    def recall(self, request: FrameworkRecallRequest) -> FrameworkRecallResult:
        payload = self._request(
            "POST",
            self._bank_path(request.identity, "/memories/recall"),
            body={
                "query": request.query,
                "budget": "mid",
                "max_tokens": request.max_tokens,
                "tags": request.identity.hindsight_tags,
                "tags_match": "all_strict",
                "query_timestamp": request.since.isoformat() if request.since else None,
            },
        )
        records = [_hindsight_record(value) for value in _mapping_list(payload.get("results"))]
        return FrameworkRecallResult(records=records[: request.top_k], total=len(records))

    def reflect(self, request: FrameworkRecallRequest) -> Mapping[str, Any]:
        return self._request(
            "POST",
            self._bank_path(request.identity, "/reflect"),
            body={
                "query": request.query,
                "budget": "low",
                "max_tokens": request.max_tokens,
                "tags": request.identity.hindsight_tags,
                "tags_match": "all_strict",
            },
        )

    def get(self, *, identity: MemoryEngineIdentity, engine_id: str) -> Mapping[str, Any] | None:
        return self._request("GET", self._bank_path(identity, f"/memories/{quote(engine_id, safe='')}"))

    def list(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]:
        payload = self._request(
            "GET",
            self._bank_path(identity, "/memories/list"),
            query={"limit": "1000"},
        )
        return _mapping_list(payload.get("items"))

    def history(self, *, identity: MemoryEngineIdentity, engine_id: str) -> list[Mapping[str, Any]]:
        payload = self._request("GET", self._bank_path(identity, f"/memories/{quote(engine_id, safe='')}/history"))
        return _mapping_list(payload.get("history") if isinstance(payload, Mapping) else None)

    def delete(self, *, identity: MemoryEngineIdentity, engine_id: str, project_memory_id: str | None = None) -> bool:
        document_id = project_memory_id or engine_id
        payload = self._request("DELETE", self._bank_path(identity, f"/documents/{quote(document_id, safe='')}"))
        return bool(payload.get("success"))

    def clear(self, *, identity: MemoryEngineIdentity) -> int:
        payload = self._request("DELETE", self._bank_path(identity, "/memories"))
        return int(payload.get("deleted_count") or 0)

    def export(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]:
        return self.list(identity=identity)


class Mem0MemoryEngineAdapter(_HttpEngineAdapter):
    name = "mem0"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        api_key: str | None = None,
        transport: FrameworkTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, transport=transport)
        self._headers = {"X-API-Key": api_key} if api_key else None

    def health(self) -> FrameworkHealthResult:
        self._request("GET", "/")
        return FrameworkHealthResult(status="ok")

    def retain(self, request: FrameworkRetainRequest) -> FrameworkRetainResult:
        payload = self._request(
            "POST",
            "/memories",
            body={
                "messages": [{"role": "user", "content": request.text}],
                **request.identity.mem0_filters,
                "metadata": _safe_metadata(request.metadata, request),
                "infer": True,
            },
            headers=self._headers,
        )
        results = _mapping_list(payload.get("results"))
        return FrameworkRetainResult(
            accepted=True,
            engine_ids=[str(item["id"]) for item in results if item.get("id")],
        )

    def recall(self, request: FrameworkRecallRequest) -> FrameworkRecallResult:
        payload = self._request(
            "POST",
            "/search",
            body={"query": request.query, "filters": request.identity.mem0_filters, "top_k": request.top_k},
            headers=self._headers,
        )
        records = [_mem0_record(value) for value in _mapping_list(payload.get("results"))]
        return FrameworkRecallResult(records=records[: request.top_k], total=len(records))

    def reflect(self, request: FrameworkRecallRequest) -> Mapping[str, Any]:
        recalled = self.recall(request)
        return {"records": [record.model_dump(mode="json") for record in recalled.records]}

    def get(self, *, identity: MemoryEngineIdentity, engine_id: str) -> Mapping[str, Any] | None:
        return self._request("GET", f"/memories/{quote(engine_id, safe='')}", headers=self._headers)

    def list(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]:
        payload = self._request("GET", "/memories", query=identity.mem0_filters, headers=self._headers)
        return _mapping_list(payload.get("results") or payload.get("memories"))

    def history(self, *, identity: MemoryEngineIdentity, engine_id: str) -> list[Mapping[str, Any]]:
        payload = self._request("GET", f"/memories/{quote(engine_id, safe='')}/history", headers=self._headers)
        return _mapping_list(payload.get("history") or payload.get("results"))

    def delete(self, *, identity: MemoryEngineIdentity, engine_id: str, project_memory_id: str | None = None) -> bool:
        payload = self._request("DELETE", f"/memories/{quote(engine_id, safe='')}", headers=self._headers)
        return bool(payload.get("success", True))

    def clear(self, *, identity: MemoryEngineIdentity) -> int:
        payload = self._request("DELETE", "/memories", body=identity.mem0_filters, headers=self._headers)
        return int(payload.get("deleted_count") or 0)

    def export(self, *, identity: MemoryEngineIdentity) -> list[Mapping[str, Any]]:
        return self.list(identity=identity)


def _safe_metadata(metadata: Mapping[str, Any], request: FrameworkRetainRequest) -> dict[str, Any]:
    reserved = {"user_id", "agent_id", "run_id", "tenant_id", "project_id", "session_id"}
    return {
        **{str(key): value for key, value in metadata.items() if str(key).lower() not in reserved},
        "project_memory_id": request.project_memory_id,
        "memory_type": request.memory_type,
        "source": request.source,
        "idempotency_key": request.idempotency_key,
    }


def _hindsight_record(value: Mapping[str, Any]) -> FrameworkMemoryRecord:
    metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
    return FrameworkMemoryRecord(
        engine_id=str(value.get("id") or value.get("memory_id")),
        project_memory_id=_optional_string(metadata.get("project_memory_id")),
        text=str(value.get("text") or value.get("content") or ""),
        memory_type=_memory_type(metadata.get("memory_type")),
        source=str(metadata.get("source") or "hindsight"),
        created_at=_datetime(value.get("occurred_start") or value.get("date")),
        metadata={key: metadata[key] for key in ("project_memory_id", "memory_type", "source") if key in metadata},
    )


def _mem0_record(value: Mapping[str, Any]) -> FrameworkMemoryRecord:
    metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
    return FrameworkMemoryRecord(
        engine_id=str(value.get("id") or value.get("memory_id")),
        project_memory_id=_optional_string(metadata.get("project_memory_id")),
        text=str(value.get("memory") or value.get("text") or ""),
        memory_type=_memory_type(metadata.get("memory_type")),
        source=str(metadata.get("source") or "mem0"),
        created_at=_datetime(value.get("created_at") or value.get("updated_at")),
        relevance=_score(value.get("score")),
        metadata={key: metadata[key] for key in ("project_memory_id", "memory_type", "source") if key in metadata},
    )


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _memory_type(value: Any) -> str:
    allowed = {"preference", "conversation", "task", "artifact", "product", "image", "video", "generation", "render"}
    return str(value) if str(value) in allowed else "task"


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))
