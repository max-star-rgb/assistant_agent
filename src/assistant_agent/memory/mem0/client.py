"""Mem0-native client used by the long-term memory service."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.transport import (
    Mem0HttpRequest,
    Mem0OperationError,
    Mem0Transport,
    urllib_mem0_transport,
)
from assistant_agent.memory.models import CompletedTurn, LongTermMemory
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.models import (
    Mem0HealthResult,
    Mem0IngestionResult,
)


class UnavailableMem0Client:
    """Offline-safe client used when no Mem0 sidecar is configured."""

    configured = False

    def health(self) -> Mem0HealthResult:
        return Mem0HealthResult(status="unavailable")

    def recall_long_term_memory(
        self,
        identity: RequestIdentity,
        *,
        top_k: int = 5,
    ) -> list[LongTermMemory]:
        _ = identity, top_k
        raise Mem0OperationError("recall", "Mem0 sidecar is not configured")

    def ingest_completed_turn(
        self,
        turn: CompletedTurn,
    ) -> Mem0IngestionResult:
        _ = turn
        raise Mem0OperationError("ingest", "Mem0 sidecar is not configured")


class Mem0Client:
    """Bind runtime identity and expose only Mem0 get-all/add operations."""

    configured = True
    _MIN_INGESTION_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        *,
        base_url: str,
        identity_namespace: str,
        timeout_seconds: float = 5.0,
        api_key: str | None = None,
        transport: Mem0Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity_namespace = identity_namespace
        self.timeout_seconds = timeout_seconds
        self.ingestion_timeout_seconds = max(
            timeout_seconds,
            self._MIN_INGESTION_TIMEOUT_SECONDS,
        )
        self._transport = transport or urllib_mem0_transport(self.base_url)
        self._headers = {"X-API-Key": api_key} if api_key else None

    def health(self) -> Mem0HealthResult:
        self._request("GET", "/")
        return Mem0HealthResult(status="ok")

    def recall_long_term_memory(
        self,
        identity: RequestIdentity,
        *,
        top_k: int = 5,
    ) -> list[LongTermMemory]:
        engine_identity = bind_mem0_identity(
            identity,
            namespace=self.identity_namespace,
        )
        payload = self._request(
            "GET",
            "/memories",
            query={
                **engine_identity.long_term_filters,
                "limit": str(top_k),
            },
        )
        return [
            _long_term_memory(value)
            for value in _mapping_list(payload.get("results"))[:top_k]
        ]

    def ingest_completed_turn(
        self,
        turn: CompletedTurn,
    ) -> Mem0IngestionResult:
        identity = bind_mem0_identity(
            turn.identity,
            namespace=self.identity_namespace,
        )
        try:
            payload = self._request(
                "POST",
                "/memories",
                body={
                    "messages": [
                        {"role": "user", "content": turn.user_text},
                        {"role": "assistant", "content": turn.assistant_text},
                    ],
                    **identity.mem0_filters,
                    "metadata": {
                        "source": "runtime_turn_ingestion",
                        "source_turn": turn.source_turn,
                        "occurred_at": turn.occurred_at.isoformat(),
                    },
                },
                timeout_seconds=self.ingestion_timeout_seconds,
            )
        except Exception:
            return Mem0IngestionResult(
                accepted=False,
                errors=[{"code": "mem0_ingestion_failed"}],
            )
        results = _mapping_list(payload.get("results"))
        return Mem0IngestionResult(
            accepted=True,
            memory_ids=[
                str(item["id"]) for item in results if item.get("id")
            ],
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        payload = self._transport(
            Mem0HttpRequest(
                method=method,
                path=path,
                body=body,
                query=query,
                headers=self._headers,
                timeout_seconds=(
                    self.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            )
        )
        if not isinstance(payload, Mapping):
            raise Mem0OperationError(path, "Mem0 returned an invalid response")
        return payload


def _long_term_memory(value: Mapping[str, Any]) -> LongTermMemory:
    return LongTermMemory(
        memory_id=str(value.get("id") or value.get("memory_id")),
        text=str(value.get("memory") or value.get("text") or ""),
        created_at=(
            _datetime(value.get("created_at") or value.get("updated_at"))
            or datetime.now().astimezone()
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
