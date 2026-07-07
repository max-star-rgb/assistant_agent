"""Adapters for external Memory Server contracts."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from pydantic import BaseModel, Field, ValidationError, model_validator

from assistant_agent.memory.store import MemoryStore
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult, MemoryType
from assistant_agent.schemas.memory_audit import MemoryPendingConfirmation
from assistant_agent.services.provider_errors import sanitize_error_message


_REMOTE_TEXT_TYPE = "text"
_REMOTE_IMAGE_TYPE = "image"
_REMOTE_SOURCE = "memory_server"
_SAFE_METADATA_KEYS = {"topic", "subtopic"}
_TASK_STATUS_SCOPE_WARNING = "memory_server_task_lookup_user_scope_not_enforced"
_UNSAFE_REMOTE_PAYLOAD_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "base64",
    "bearer",
    "cookie",
    "image_base64",
    "password",
    "provider_response",
    "raw",
    "raw_audio",
    "raw_image",
    "raw_media",
    "raw_payload",
    "raw_provider_payload",
    "raw_provider_response",
    "raw_video",
    "secret",
    "token",
    "video_base64",
}


@dataclass(frozen=True)
class MemoryServerRequest:
    """One JSON request to the external Memory Server."""

    method: str
    path: str
    body: Mapping[str, Any] | None = None
    timeout_seconds: float = 2.0


MemoryServerTransport = Callable[[MemoryServerRequest], Mapping[str, Any]]


class MemoryServerMediaFile(BaseModel):
    """Safe file reference accepted by the external media ingestion API."""

    file_id: str = Field(min_length=1)
    file_url: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    start_time: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metadata_is_trace_safe(self) -> "MemoryServerMediaFile":
        _reject_unsafe_upload_metadata(self.metadata)
        self.metadata = _safe_mapping_payload(self.metadata)
        return self


class MemoryServerUploadResult(BaseModel):
    """Structured result from `/v1/media/upload`."""

    task_id: str = ""
    status: str
    accepted_count: int = Field(default=0, ge=0)
    code: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)


class MemoryServerTaskStatusResult(BaseModel):
    """Structured result from `/v1/tasks_status`."""

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
    scope_warning: str = _TASK_STATUS_SCOPE_WARNING


class RemoteMemoryClient:
    """Small client for the external Memory Server HTTP contract."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 2.0,
        query_strategy: str = "vector",
        include_media_chunks: bool = False,
        direct_answer: bool = False,
        trace: bool = False,
        transport: MemoryServerTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.query_strategy = query_strategy
        self.include_media_chunks = include_media_chunks
        self.direct_answer = direct_answer
        self.trace = trace
        self._transport = transport or _urllib_transport(self.base_url)

    def health(self, *, user_id: str | None = None, session_id: str | None = None) -> Mapping[str, Any]:
        """Return the remote health payload."""

        params = {
            key: value
            for key, value in {
                "user_id": user_id,
                "session_id": session_id,
            }.items()
            if value
        }
        suffix = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._transport(
            MemoryServerRequest(
                method="GET",
                path=f"/v1/health{suffix}",
                timeout_seconds=self.timeout_seconds,
            )
        )

    def query_memories(self, query: MemoryQuery) -> MemorySearchResult:
        """Query the remote service and return internal memory search results."""

        body: dict[str, Any] = {
            "user_id": query.user_id,
            "query": query.query,
            "top_k": query.top_k,
            "direct_answer": self.direct_answer,
            "options": {
                "strategy": self.query_strategy,
                "include_media_chunks": self.include_media_chunks,
                "trace": self.trace,
            },
        }
        if query.session_id:
            body["session_id"] = query.session_id
        if query.since is not None:
            body["after_timestamp"] = query.since.isoformat()
        try:
            response = self._transport(
                MemoryServerRequest(
                    method="POST",
                    path="/v1/memories/query",
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except Exception as exc:
            return MemorySearchResult(
                items=[],
                query_used=query,
                total=0,
                ranking_reason="memory_server_remote_query_failed",
                memory_context="",
                errors=[
                    {
                        "code": "memory_server_query_failed",
                        "message": "memory server query failed",
                        "recoverable": True,
                        "detail": sanitize_error_message(str(exc)),
                    }
                ],
            )
        return memory_search_result_from_memory_server_response(response, query)

    def upload_media(
        self,
        *,
        user_id: str,
        session_id: str,
        files: list[MemoryServerMediaFile],
    ) -> MemoryServerUploadResult:
        """Submit safe media file references to the external ingestion API."""

        if not files:
            return MemoryServerUploadResult(
                task_id="",
                status="failed",
                accepted_count=0,
                code=0,
                errors=[
                    {
                        "code": "memory_server_upload_invalid",
                        "message": "memory server upload requires at least one file",
                        "recoverable": True,
                    }
                ],
            )
        body = {
            "user_id": user_id,
            "session_id": session_id,
            "files": [_media_file_request_body(file) for file in files],
        }
        try:
            response = self._transport(
                MemoryServerRequest(
                    method="POST",
                    path="/v1/media/upload",
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except Exception as exc:
            return MemoryServerUploadResult(
                task_id="",
                status="failed",
                accepted_count=0,
                code=0,
                errors=[
                    {
                        "code": "memory_server_upload_failed",
                        "message": "memory server upload failed",
                        "recoverable": True,
                        "detail": sanitize_error_message(str(exc)),
                    }
                ],
            )
        return _upload_result_from_memory_server_response(response)

    def task_status(self, *, user_id: str, task_id: str) -> MemoryServerTaskStatusResult:
        """Return external media ingestion task status with a user-scope warning."""

        try:
            response = self._transport(
                MemoryServerRequest(
                    method="POST",
                    path="/v1/tasks_status",
                    body={"user_id": user_id, "task_id": task_id},
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except Exception as exc:
            return MemoryServerTaskStatusResult(
                task_id=task_id,
                status="failed",
                errors=[
                    {
                        "code": "memory_server_task_status_failed",
                        "message": "memory server task status failed",
                        "recoverable": True,
                        "detail": sanitize_error_message(str(exc)),
                    }
                ],
            )
        return _task_status_result_from_memory_server_response(response, task_id=task_id)


class HybridMemoryStore:
    """Memory store that keeps lifecycle local and augments search remotely."""

    def __init__(self, *, local_store: MemoryStore, remote_client: RemoteMemoryClient) -> None:
        self.local_store = local_store
        self.remote_client = remote_client

    def save(self, item: MemoryItem) -> MemoryItem:
        return self.local_store.save(item)

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        local_result = self.local_store.search(query)
        remote_result = self.remote_client.query_memories(query)
        items = _merge_memory_items(local_result.items, remote_result.items, top_k=query.top_k)
        errors = [*local_result.errors, *remote_result.errors]
        return MemorySearchResult(
            items=items,
            query_used=query,
            total=len(items),
            ranking_reason="hybrid_local_then_memory_server",
            memory_context=_format_remote_memory_context(items, query.max_context_chars),
            errors=errors,
        )

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        return self.local_store.get(user_id, memory_id)

    def delete(self, user_id: str, memory_id: str) -> bool:
        return self.local_store.delete(user_id, memory_id)

    def hard_delete(self, user_id: str, memory_id: str) -> bool:
        return self.local_store.hard_delete(user_id, memory_id)

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        return self.local_store.delete_by_session(user_id, session_id)

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        return self.local_store.list_by_user(user_id)

    def clear_user(self, user_id: str) -> None:
        self.local_store.clear_user(user_id)

    def save_confirmation(self, confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
        return self.local_store.save_confirmation(confirmation)

    def get_confirmation(self, user_id: str, confirmation_id: str) -> MemoryPendingConfirmation | None:
        return self.local_store.get_confirmation(user_id, confirmation_id)

    def list_confirmations(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        include_resolved: bool = True,
        limit: int = 1000,
    ) -> list[MemoryPendingConfirmation]:
        return self.local_store.list_confirmations(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            include_resolved=include_resolved,
            limit=limit,
        )

    def delete_confirmation(self, user_id: str, confirmation_id: str) -> bool:
        return self.local_store.delete_confirmation(user_id, confirmation_id)


def memory_search_result_from_memory_server_response(
    response: Mapping[str, Any],
    query: MemoryQuery,
    *,
    received_at: datetime | None = None,
) -> MemorySearchResult:
    """Convert a Memory Server query response into internal memory contracts."""

    resolved_received_at = received_at or datetime.now(timezone.utc)
    results = response.get("results")
    remote_results = results if isinstance(results, list) else []
    keyframe_refs = _keyframe_refs_by_memory_id(remote_results)

    items: list[MemoryItem] = []
    errors: list[dict[str, Any]] = []
    for result in remote_results:
        if not isinstance(result, Mapping) or result.get("type") != _REMOTE_TEXT_TYPE:
            continue
        try:
            items.append(
                _memory_item_from_remote_text_result(
                    result,
                    query,
                    keyframe_refs=keyframe_refs,
                    received_at=resolved_received_at,
                )
            )
        except (TypeError, ValueError, ValidationError):
            errors.append(
                {
                    "code": "memory_server_result_rejected",
                    "message": "remote memory result rejected",
                    "recoverable": True,
                    "memory_id": _remote_memory_id(result) or "",
                }
            )

    return MemorySearchResult(
        items=items,
        query_used=query,
        total=len(items),
        ranking_reason="memory_server_remote_query",
        memory_context=_format_remote_memory_context(items, query.max_context_chars),
        errors=errors,
    )


def _memory_item_from_remote_text_result(
    result: Mapping[str, Any],
    query: MemoryQuery,
    *,
    keyframe_refs: Mapping[str, list[str]],
    received_at: datetime,
) -> MemoryItem:
    memory_id = _remote_memory_id(result)
    if not memory_id:
        raise ValueError("remote memory id is required")
    raw_summary = str(result.get("content") or "").strip()
    if not raw_summary:
        raise ValueError("remote memory summary is required")
    summary = sanitize_error_message(raw_summary)
    source = _mapping(result.get("source"))
    metadata = _mapping(result.get("metadata"))
    remote_memory_type = str(result.get("memory_type") or "task")
    timestamp_start = _optional_string(source.get("timestamp_start"))
    timestamp_end = _optional_string(source.get("timestamp_end"))

    content = _safe_remote_content(
        remote_memory_type=remote_memory_type,
        source=source,
        metadata=metadata,
        timestamp_start=timestamp_start,
        timestamp_end=timestamp_end,
    )
    artifact_refs = _artifact_refs_for_result(result, keyframe_refs.get(memory_id, []))
    tags = _safe_tags(remote_memory_type, metadata)

    return MemoryItem(
        memory_id=f"{_REMOTE_SOURCE}:{memory_id}",
        user_id=query.user_id,
        tenant_id=query.tenant_id,
        project_id=query.project_id,
        session_id=query.session_id,
        memory_type=_internal_memory_type(remote_memory_type),
        content=content,
        summary=summary,
        tags=tags,
        source=_REMOTE_SOURCE,
        artifact_refs=artifact_refs,
        relevance=_bounded_relevance(result.get("score")),
        reason="retrieved from external Memory Server",
        created_at=_parse_datetime(timestamp_start) or received_at,
    )


def _media_file_request_body(file: MemoryServerMediaFile) -> dict[str, Any]:
    return {
        "file_id": file.file_id,
        "file_url": file.file_url,
        "filename": file.filename,
        "media_type": file.media_type,
        "start_time": _format_memory_server_datetime(file.start_time),
        "metadata": file.metadata,
    }


def _upload_result_from_memory_server_response(response: Mapping[str, Any]) -> MemoryServerUploadResult:
    return MemoryServerUploadResult(
        task_id=str(response.get("task_id") or ""),
        status=str(response.get("status") or "unknown"),
        accepted_count=_safe_int(response.get("accepted_count")),
        code=_safe_int(response.get("code")),
        errors=_safe_error_list(response.get("errors")),
    )


def _task_status_result_from_memory_server_response(
    response: Mapping[str, Any],
    *,
    task_id: str,
) -> MemoryServerTaskStatusResult:
    code = _safe_int(response.get("code"))
    error = _optional_string(response.get("error"))
    errors = _safe_error_list(response.get("errors"))
    if error:
        errors.append(
            {
                "code": "memory_server_task_status_error",
                "message": sanitize_error_message(error),
                "recoverable": code != 200,
            }
        )
    return MemoryServerTaskStatusResult(
        task_id=str(response.get("task_id") or task_id),
        status=str(response.get("status") or ("not_found" if code == 404 else "unknown")),
        total_files=_safe_int(response.get("total_files")),
        processed_files=_safe_int(response.get("processed_files")),
        failed_files=_safe_int(response.get("failed_files")),
        estimated_completion_seconds=_safe_optional_float(response.get("estimated_completion_seconds")),
        statistics=_safe_mapping_payload(_mapping(response.get("statistics"))),
        results=_safe_mapping_list(response.get("results")),
        errors=errors,
        code=code,
    )


def _keyframe_refs_by_memory_id(results: list[Any]) -> dict[str, list[str]]:
    refs_by_memory_id: dict[str, list[str]] = {}
    for result in results:
        if not isinstance(result, Mapping) or result.get("type") != _REMOTE_IMAGE_TYPE:
            continue
        memory_id = _remote_memory_id(result)
        if not memory_id:
            continue
        refs = _artifact_refs_for_result(result, [])
        if refs:
            refs_by_memory_id.setdefault(memory_id, [])
            for ref in refs:
                if ref not in refs_by_memory_id[memory_id]:
                    refs_by_memory_id[memory_id].append(ref)
    return refs_by_memory_id


def _artifact_refs_for_result(result: Mapping[str, Any], related_refs: list[str]) -> list[str]:
    refs: list[str] = []
    for value in [result.get("image_url"), _mapping(result.get("media")).get("url"), *related_refs]:
        ref = _safe_artifact_ref(value)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _safe_remote_content(
    *,
    remote_memory_type: str,
    source: Mapping[str, Any],
    metadata: Mapping[str, Any],
    timestamp_start: str | None,
    timestamp_end: str | None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "remote_memory_type": remote_memory_type,
    }
    for key in ("source_id",):
        value = _optional_string(source.get(key))
        if value:
            content[key] = value
    if timestamp_start:
        content["timestamp_start"] = timestamp_start
    if timestamp_end:
        content["timestamp_end"] = timestamp_end
    for key in _SAFE_METADATA_KEYS:
        value = _optional_string(metadata.get(key))
        if value:
            content[key] = sanitize_error_message(value)
    return content


def _safe_tags(remote_memory_type: str, metadata: Mapping[str, Any]) -> list[str]:
    tags = [_REMOTE_SOURCE, remote_memory_type]
    for key in ("topic", "subtopic"):
        value = _optional_string(metadata.get(key))
        if value:
            tags.append(sanitize_error_message(value))
    return list(dict.fromkeys(tags))


def _internal_memory_type(remote_memory_type: str) -> MemoryType:
    normalized = remote_memory_type.strip().lower()
    if normalized == "spatial":
        return "video"
    if normalized in {"video", "image", "product", "preference", "artifact", "generation", "render"}:
        return normalized  # type: ignore[return-value]
    if normalized == "conversation":
        return "conversation"
    return "task"


def _remote_memory_id(result: Mapping[str, Any]) -> str:
    source = _mapping(result.get("source"))
    value = source.get("memory_id")
    if value is None:
        value = result.get("memory_id")
    return str(value).strip() if value is not None else ""


def _bounded_relevance(value: Any) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, score))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_memory_server_datetime(value: datetime) -> str:
    resolved = value
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    resolved = resolved.astimezone(timezone.utc)
    return resolved.isoformat().replace("+00:00", "Z")


def _safe_artifact_ref(value: Any) -> str | None:
    ref = _optional_string(value)
    if not ref:
        return None
    if ref.lower().startswith(("data:image/", "data:video/", "data:audio/")):
        return None
    return sanitize_error_message(ref)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_error_list(value: Any) -> list[dict[str, Any]]:
    return _safe_mapping_list(value)


def _safe_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            items.append(_safe_mapping_payload(item))
        else:
            items.append({"message": sanitize_error_message(str(item))})
    return items


def _safe_mapping_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, nested in value.items():
        normalized = str(key).lower()
        if normalized in _UNSAFE_REMOTE_PAYLOAD_KEYS:
            continue
        payload[str(key)] = _safe_payload_value(nested)
    return payload


def _safe_payload_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _safe_mapping_payload(value)
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_error_message(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return _format_memory_server_datetime(value)
    return sanitize_error_message(str(value))


def _reject_unsafe_upload_metadata(value: Mapping[str, Any]) -> None:
    for key, nested in value.items():
        normalized = str(key).lower()
        if normalized in _UNSAFE_REMOTE_PAYLOAD_KEYS:
            raise ValueError(f"unsafe media upload metadata key: {key}")
        if isinstance(nested, Mapping):
            _reject_unsafe_upload_metadata(nested)
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, Mapping):
                    _reject_unsafe_upload_metadata(item)


def _format_remote_memory_context(items: list[MemoryItem], max_chars: int) -> str:
    context = "\n".join(item.summary for item in items)
    return context[:max_chars]


def _merge_memory_items(
    local_items: list[MemoryItem],
    remote_items: list[MemoryItem],
    *,
    top_k: int,
) -> list[MemoryItem]:
    items: list[MemoryItem] = []
    seen: set[str] = set()
    for item in [*local_items, *remote_items]:
        if item.memory_id in seen:
            continue
        seen.add(item.memory_id)
        items.append(item)
        if len(items) >= top_k:
            break
    return items


def _urllib_transport(base_url: str) -> MemoryServerTransport:
    def transport(request: MemoryServerRequest) -> Mapping[str, Any]:
        url = f"{base_url}{request.path}"
        payload = json.dumps(request.body).encode("utf-8") if request.body is not None else None
        http_request = urllib.request.Request(
            url,
            data=payload,
            method=request.method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                data = response.read().decode("utf-8")
        except socket.timeout as exc:
            raise TimeoutError("memory server request timed out") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"memory server request failed: {exc.reason}") from exc
        try:
            decoded = json.loads(data or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("memory server returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError("memory server returned a non-object response")
        return decoded

    return transport
