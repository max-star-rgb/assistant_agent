"""REST-only Mem0 adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from assistant_agent.memory.mem0.base import (
    Mem0HttpRequest,
    Mem0OperationError,
)
from assistant_agent.memory.mem0.http import (
    Mem0Transport,
    urllib_mem0_transport,
)
from assistant_agent.schemas.mem0 import (
    Mem0HealthResult,
    Mem0MemoryRecord,
    Mem0RecallRequest,
    Mem0RecallResult,
    Mem0TurnCaptureRequest,
    Mem0TurnCaptureResult,
)


class UnavailableMem0Adapter:
    """Offline-safe Mem0 adapter used when no sidecar is configured."""

    name = "mem0"
    configured = False

    def health(self) -> Mem0HealthResult:
        return Mem0HealthResult(status="unavailable")

    def capture_turn(
        self,
        request: Mem0TurnCaptureRequest,
    ) -> Mem0TurnCaptureResult:
        _ = request
        raise Mem0OperationError(
            "capture_turn",
            "Mem0 sidecar is not configured",
        )

    def recall(self, request: Mem0RecallRequest) -> Mem0RecallResult:
        _ = request
        raise Mem0OperationError(
            "recall",
            "Mem0 sidecar is not configured",
        )


class Mem0RestAdapter:
    name = "mem0"
    configured = True

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        api_key: str | None = None,
        transport: Mem0Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or urllib_mem0_transport(self.base_url)
        self._headers = {"X-API-Key": api_key} if api_key else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        payload = self._transport(
            Mem0HttpRequest(
                method=method,
                path=path,
                body=body,
                query=query,
                headers=self._headers,
                timeout_seconds=self.timeout_seconds,
            )
        )
        if not isinstance(payload, Mapping):
            raise Mem0OperationError(
                path,
                "Mem0 returned an invalid response",
            )
        return payload

    def health(self) -> Mem0HealthResult:
        self._request("GET", "/")
        return Mem0HealthResult(status="ok")

    def capture_turn(
        self,
        request: Mem0TurnCaptureRequest,
    ) -> Mem0TurnCaptureResult:
        identity = request.identity.mem0_filters
        try:
            payload = self._request(
                "POST",
                "/memories",
                body={
                    "messages": [
                        message.model_dump(mode="json")
                        for message in request.messages
                    ],
                    **identity,
                    "metadata": {
                        "source": "runtime_turn_capture",
                        "source_turn": request.source_turn,
                        "occurred_at": request.occurred_at.isoformat(),
                    },
                },
            )
        except Exception:
            return Mem0TurnCaptureResult(
                accepted=False,
                errors=[{"code": "mem0_capture_failed"}],
            )
        results = _mapping_list(payload.get("results"))
        return Mem0TurnCaptureResult(
            accepted=True,
            memory_ids=[
                str(item["id"]) for item in results if item.get("id")
            ],
        )

    def recall(self, request: Mem0RecallRequest) -> Mem0RecallResult:
        payload = self._request(
            "GET",
            "/memories",
            query={
                **request.identity.long_term_filters,
                "limit": str(request.top_k),
            },
        )
        records = [
            _mem0_record(value)
            for value in _mapping_list(payload.get("results"))
        ]
        return Mem0RecallResult(
            records=records[: request.top_k],
            total=len(records),
        )


def _mem0_record(value: Mapping[str, Any]) -> Mem0MemoryRecord:
    return Mem0MemoryRecord(
        engine_id=str(value.get("id") or value.get("memory_id")),
        text=str(value.get("memory") or value.get("text") or ""),
        created_at=_datetime(
            value.get("created_at") or value.get("updated_at")
        ),
        relevance=_score(value.get("score")),
    )


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


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
