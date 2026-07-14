"""MemoryStore lifecycle owner backed by a framework sidecar and governance ledger."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from assistant_agent.memory.framework.base import MemoryEngineAdapter, bind_engine_identity
from assistant_agent.memory.framework.ledger import (
    FrameworkGovernanceLedger,
    FrameworkRetryReport,
)
from assistant_agent.memory.remote import MemoryServiceOperationError
from assistant_agent.memory.retrieval import format_memory_context
from assistant_agent.memory.store import MemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from assistant_agent.schemas.memory_audit import MemoryAuditEvent, MemoryPendingConfirmation
from assistant_agent.schemas.memory_framework import (
    FrameworkMemoryRecord,
    FrameworkRecallRequest,
    FrameworkRetainRequest,
    FrameworkRetainResult,
)


class FrameworkMemoryStore:
    """Framework lifecycle owner; optional v2 store is read-only fallback."""

    framework_managed_algorithms = True

    def __init__(
        self,
        *,
        adapter: MemoryEngineAdapter,
        ledger: FrameworkGovernanceLedger,
        identity_namespace: str,
        read_fallback: MemoryStore | None = None,
    ) -> None:
        self.adapter = adapter
        self.ledger = ledger
        self.identity_namespace = identity_namespace
        self.read_fallback = read_fallback

    def save(self, item: MemoryItem) -> MemoryItem:
        request = self._retain_request(item)
        queued = False
        try:
            result = self._call("retain", lambda: self.adapter.retain(request))
            if not result.accepted:
                raise MemoryServiceOperationError("retain", "memory framework rejected retain")
            self._record_retain_mappings(
                item.user_id,
                request,
                result,
                tenant_id=item.tenant_id,
                project_id=item.project_id,
                session_id=item.session_id,
            )
        except Exception:
            queued = True
            self.ledger.enqueue(
                operation="retain",
                idempotency_key=request.idempotency_key,
                payload={
                    "user_id": item.user_id,
                    "tenant_id": item.tenant_id,
                    "project_id": item.project_id,
                    "session_id": item.session_id,
                    "request": request.model_dump(mode="json"),
                },
            )
        if queued:
            return item.model_copy(
                update={
                    "content": {
                        **item.content,
                        "_framework_retain_status": "queued",
                        "_framework_durable_outbox": True,
                    }
                }
            )
        return item

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        identity = self._identity_for_query(query)
        try:
            recalled = self._call(
                "recall",
                lambda: self.adapter.recall(
                    FrameworkRecallRequest(
                        identity=identity,
                        query=query.query.strip() or "recent memories",
                        top_k=query.top_k,
                        memory_types=query.memory_types,
                        since=query.since,
                        max_tokens=max(64, min(8192, query.max_context_chars // 2)),
                    )
                ),
            )
        except Exception:
            return self._fallback_search(query)
        mapped_ids = {
            mapping.engine_id: mapping.project_memory_id
            for mapping in self.ledger.list_mappings(user_id=query.user_id)
            if (mapping.tenant_id is None or mapping.tenant_id == query.tenant_id)
            and (mapping.project_id is None or mapping.project_id == query.project_id)
        }
        records = [
            record.model_copy(
                update={"project_memory_id": mapped_ids.get(record.engine_id)}
            )
            if record.project_memory_id is None and record.engine_id in mapped_ids
            else record
            for record in recalled.records
        ]
        items = [
            item
            for record in records
            if (item := self._item_from_record(record, query)) is not None
        ]
        return MemorySearchResult(
            items=items,
            query_used=query,
            total=len(items),
            ranking_reason=f"framework_{self.adapter.name}_managed_recall",
            memory_context=format_memory_context(items, max_chars=query.max_context_chars),
        )

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        return next((item for item in self.list_by_user(user_id) if item.memory_id == memory_id), None)

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        seen: set[str] = set()
        try:
            for mapping in self.ledger.list_mappings(user_id=user_id):
                if mapping.project_memory_id in seen or self.ledger.is_tombstoned(
                    user_id=user_id,
                    project_memory_id=mapping.project_memory_id,
                    engine_id=mapping.engine_id,
                ):
                    continue
                payload = self.adapter.get(identity=mapping.identity, engine_id=mapping.engine_id)
                if not payload:
                    continue
                record = _record_from_payload(payload, mapping.engine_id, mapping.project_memory_id)
                query = MemoryQuery(
                    user_id=user_id,
                    tenant_id=mapping.tenant_id,
                    project_id=mapping.project_id,
                    session_id=mapping.session_id,
                    query="",
                    top_k=50,
                )
                item = self._item_from_record(record, query)
                if item is not None:
                    items.append(item)
                    seen.add(item.memory_id)
        except Exception:
            if self.read_fallback is not None:
                return self.read_fallback.list_by_user(user_id)
            return []
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def delete(self, user_id: str, memory_id: str) -> bool:
        return self._delete(user_id=user_id, memory_id=memory_id, identity=None)

    def delete_for_identity(self, identity: RequestIdentity, memory_id: str) -> bool:
        bound = bind_engine_identity(identity, namespace=self.identity_namespace)
        return self._delete(user_id=identity.user_id, memory_id=memory_id, identity=bound)

    def _delete(self, *, user_id: str, memory_id: str, identity) -> bool:
        mappings = self.ledger.list_mappings(user_id=user_id, project_memory_id=memory_id)
        if identity is not None:
            mappings = [
                mapping for mapping in mappings
                if mapping.identity.user_id == identity.user_id
                and mapping.identity.agent_id == identity.agent_id
                and mapping.identity.tenant_tag == identity.tenant_tag
            ]
        if not mappings:
            cancelled = self.ledger.cancel_pending_retain(
                user_id=user_id,
                project_memory_id=memory_id,
                identity=identity,
            )
            if cancelled:
                self.ledger.record_tombstone(
                    user_id=user_id,
                    project_memory_id=memory_id,
                    engine_id="pending",
                )
                return True
            return False
        for mapping in mappings:
            self.ledger.record_tombstone(
                user_id=user_id,
                project_memory_id=memory_id,
                engine_id=mapping.engine_id,
            )
            payload = {
                "user_id": user_id,
                "project_memory_id": memory_id,
                "engine_id": mapping.engine_id,
                "identity": mapping.identity.model_dump(mode="json"),
            }
            try:
                self._call(
                    "delete",
                    lambda mapping=mapping: self.adapter.delete(
                        identity=mapping.identity,
                        engine_id=mapping.engine_id,
                        project_memory_id=mapping.project_memory_id,
                    ),
                )
            except Exception:
                self.ledger.enqueue(
                    operation="delete",
                    idempotency_key=f"delete:{self.adapter.name}:{user_id}:{memory_id}:{mapping.engine_id}",
                    payload=payload,
                )
        return True

    def hard_delete(self, user_id: str, memory_id: str) -> bool:
        return self.delete(user_id, memory_id)

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        mappings = [
            mapping for mapping in self.ledger.list_mappings(user_id=user_id)
            if mapping.identity.run_id == bind_engine_identity(
                RequestIdentity.for_user(user_id=user_id, session_id=session_id),
                namespace=self.identity_namespace,
            ).run_id
        ]
        memory_ids = sorted({mapping.project_memory_id for mapping in mappings})
        return sum(1 for memory_id in memory_ids if self.delete(user_id, memory_id))

    def clear_user(self, user_id: str) -> None:
        for memory_id in sorted({mapping.project_memory_id for mapping in self.ledger.list_mappings(user_id=user_id)}):
            self.delete(user_id, memory_id)
        for entry in self.ledger.pending_outbox(limit=10_000):
            if entry.operation == "retain" and entry.payload.get("user_id") == user_id:
                request = entry.payload.get("request")
                if isinstance(request, dict) and request.get("project_memory_id"):
                    self.delete(user_id, str(request["project_memory_id"]))
        self.ledger.clear_confirmations(user_id=user_id)

    def retry_outbox(self, *, limit: int = 100) -> FrameworkRetryReport:
        attempted = succeeded = failed = 0
        for entry in self.ledger.pending_outbox(limit=limit):
            attempted += 1
            try:
                if entry.operation == "retain":
                    request = FrameworkRetainRequest.model_validate(entry.payload["request"])
                    result = self.adapter.retain(request)
                    if not result.accepted:
                        raise MemoryServiceOperationError("retain", "memory framework rejected retain")
                    self._record_retain_mappings(
                        str(entry.payload["user_id"]),
                        request,
                        result,
                        tenant_id=entry.payload.get("tenant_id"),
                        project_id=entry.payload.get("project_id"),
                        session_id=entry.payload.get("session_id"),
                    )
                elif entry.operation == "delete":
                    request_identity = entry.payload["identity"]
                    from assistant_agent.schemas.memory_framework import MemoryEngineIdentity

                    self.adapter.delete(
                        identity=MemoryEngineIdentity.model_validate(request_identity),
                        engine_id=str(entry.payload["engine_id"]),
                        project_memory_id=str(entry.payload["project_memory_id"]),
                    )
                self.ledger.complete_outbox(entry.outbox_id)
                succeeded += 1
            except Exception:
                self.ledger.fail_outbox(entry.outbox_id, error_code="memory_framework_retry_failed")
                failed += 1
        return FrameworkRetryReport(attempted=attempted, succeeded=succeeded, failed=failed)

    def save_confirmation(self, confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
        return self.ledger.save_confirmation(confirmation)

    def get_confirmation(self, user_id: str, confirmation_id: str) -> MemoryPendingConfirmation | None:
        return self.ledger.get_confirmation(user_id=user_id, confirmation_id=confirmation_id)

    def list_confirmations(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        include_resolved: bool = True,
        limit: int = 1000,
    ) -> list[MemoryPendingConfirmation]:
        return self.ledger.list_confirmations(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            include_resolved=include_resolved,
            limit=limit,
        )

    def delete_confirmation(self, user_id: str, confirmation_id: str) -> bool:
        return self.ledger.delete_confirmation(user_id=user_id, confirmation_id=confirmation_id)

    def save_audit_event(self, event: MemoryAuditEvent) -> MemoryAuditEvent:
        return self.ledger.save_audit_event(event)

    def list_audit_events(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[MemoryAuditEvent]:
        return self.ledger.list_audit_events(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            event_type=event_type,
            limit=limit,
        )

    def _retain_request(self, item: MemoryItem) -> FrameworkRetainRequest:
        identity = bind_engine_identity(
            RequestIdentity.for_user(
                tenant_id=item.tenant_id,
                user_id=item.user_id,
                project_id=item.project_id,
                session_id=item.session_id,
            ),
            namespace=self.identity_namespace,
        )
        return FrameworkRetainRequest(
            identity=identity,
            project_memory_id=item.memory_id,
            text=item.summary,
            memory_type=item.memory_type,
            source=item.source,
            created_at=item.created_at,
            metadata={"tags": item.tags, "scope": item.scope} if item.tags or item.scope else {},
            idempotency_key=f"retain:{self.adapter.name}:{item.user_id}:{item.memory_id}",
        )

    def _record_retain_mappings(
        self,
        user_id: str,
        request: FrameworkRetainRequest,
        result: FrameworkRetainResult,
        *,
        tenant_id: str | None,
        project_id: str | None,
        session_id: str | None,
    ) -> None:
        engine_ids = result.engine_ids or [request.project_memory_id]
        for engine_id in engine_ids:
            self.ledger.record_mapping(
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                project_memory_id=request.project_memory_id,
                engine_id=engine_id,
                engine_name=self.adapter.name,
                identity=request.identity,
            )

    def _identity_for_query(self, query: MemoryQuery):
        return bind_engine_identity(
            RequestIdentity.for_user(
                tenant_id=query.tenant_id,
                user_id=query.user_id,
                project_id=query.project_id,
                session_id=query.session_id,
                allowed_scopes=list(query.allowed_scopes),
            ),
            namespace=self.identity_namespace,
        )

    def _item_from_record(self, record: FrameworkMemoryRecord, query: MemoryQuery) -> MemoryItem | None:
        memory_id = record.project_memory_id or record.engine_id
        if self.ledger.is_tombstoned(
            user_id=query.user_id,
            project_memory_id=memory_id,
            engine_id=record.engine_id,
        ):
            return None
        return MemoryItem(
            memory_id=memory_id,
            tenant_id=query.tenant_id,
            user_id=query.user_id,
            project_id=query.project_id,
            session_id=query.session_id,
            memory_type=record.memory_type,
            summary=record.text,
            content={"framework": self.adapter.name, "engine_id": record.engine_id},
            source=record.source,
            created_at=record.created_at or datetime.now(timezone.utc),
            relevance=record.relevance,
        )

    def _fallback_search(self, query: MemoryQuery) -> MemorySearchResult:
        if self.read_fallback is None:
            result = MemorySearchResult(
                items=[], query_used=query, total=0, ranking_reason="framework_unavailable", memory_context=""
            )
        else:
            result = self.read_fallback.search(query)
            result = result.model_copy(update={"ranking_reason": f"framework_degraded_to_v2:{result.ranking_reason}"})
        result.errors.append(
            {
                "code": "memory_framework_recall_failed",
                "message": "memory framework recall unavailable",
                "recoverable": True,
            }
        )
        return result

    def _call(self, operation: str, callback):
        started = time.perf_counter()
        try:
            value = callback()
        except Exception:
            self.ledger.record_call(
                operation=operation,
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000,
                error_code=f"memory_framework_{operation}_failed",
            )
            raise
        self.ledger.record_call(
            operation=operation,
            status="ok",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return value


def _record_from_payload(payload: dict[str, Any], engine_id: str, project_memory_id: str) -> FrameworkMemoryRecord:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return FrameworkMemoryRecord(
        engine_id=engine_id,
        project_memory_id=project_memory_id,
        text=str(payload.get("memory") or payload.get("text") or payload.get("content") or "memory"),
        memory_type=str(metadata.get("memory_type") or "task"),
        source=str(metadata.get("source") or "memory_framework"),
        created_at=payload.get("created_at"),
        relevance=payload.get("score"),
    )
