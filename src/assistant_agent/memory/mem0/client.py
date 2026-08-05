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
    Mem0MemoryChange,
)


class UnavailableMem0Client:
    """Offline-safe client used when no Mem0 sidecar is configured."""

    configured = False

    def health(self) -> Mem0HealthResult:
        return Mem0HealthResult(status="unavailable")

    def recall_long_term_memory(
        self,
        identity: RequestIdentity,
    ) -> list[LongTermMemory]:
        _ = identity
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
    ) -> list[LongTermMemory]:
        engine_identity = bind_mem0_identity(
            identity,
            namespace=self.identity_namespace,
        )
        payload = self._request(
            "GET",
            "/memories",
            query=engine_identity.long_term_filters,
        )
        return [
            _long_term_memory(value)
            for value in _mapping_list(payload.get("results"))
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
        changes = [
            change
            for item in _mapping_list(payload.get("results"))
            if (change := _memory_change(item)) is not None
        ]
        return Mem0IngestionResult(
            accepted=True,
            memory_ids=[change.memory_id for change in changes],
            changes=changes,
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


def _memory_change(value: Mapping[str, Any]) -> Mem0MemoryChange | None:
    memory_id = value.get("id")
    raw_event = value.get("event")
    if (
        not isinstance(memory_id, str)
        or not memory_id.strip()
        or not isinstance(raw_event, str)
    ):
        return None
    event = raw_event.upper()
    if event not in {"ADD", "UPDATE", "DELETE"}:
        return None
    memory = value.get("memory")
    if memory is not None and not isinstance(memory, str):
        return None
    if event != "DELETE" and (memory is None or not memory):
        return None
    return Mem0MemoryChange(
        memory_id=memory_id,
        memory=memory or None,
        event=event,
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
