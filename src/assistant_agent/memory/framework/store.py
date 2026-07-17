"""MemoryStore lifecycle owner backed by a framework sidecar and governance ledger."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from assistant_agent.memory.framework.base import MemoryEngineAdapter, bind_engine_identity
from assistant_agent.memory.framework.ledger import (
    FrameworkGovernanceLedger,
    FrameworkMemoryMapping,
    FrameworkRetryReport,
)
from assistant_agent.memory.remote import MemoryServiceOperationError
from assistant_agent.memory.retrieval import format_memory_context
from assistant_agent.memory.store import MemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import (
    MemoryItem,
    MemoryQuery,
    MemoryScope,
    MemorySearchResult,
    memory_scope_for_item,
)
from assistant_agent.schemas.memory_audit import MemoryAuditEvent, MemoryPendingConfirmation
from assistant_agent.schemas.memory_framework import (
    FrameworkMemoryRecord,
    FrameworkRecallResult,
    FrameworkRecallRequest,
    FrameworkRetainRequest,
    FrameworkRetainResult,
)

_MEM0_RECALL_CONSISTENCY_TIMEOUT_SECONDS = 5.0
_MEM0_RECALL_CONSISTENCY_POLL_SECONDS = 0.25
_MEM0_RECENT_RETAIN_CACHE_SECONDS = 60.0
_ENGINE_AGENT_SCOPES: set[MemoryScope] = {"project", "task", "video", "product"}
_VALID_ENGINE_SCOPES: set[MemoryScope] = {"session", "project", "task", "user_profile", "video", "product"}


class FrameworkMemoryStore:
    """Framework lifecycle owner; optional v2 store is read-only fallback."""

    framework_managed_algorithms = True
    requires_identity_session = True

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
        self._recent_retain_scopes: dict[tuple[str, str, str, str, str, str], float] = {}
        self._recent_retain_items: dict[tuple[str, str, str, str, str, str], list[tuple[float, MemoryItem]]] = {}

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
                    "scope": request.scope,
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
        recall_scopes = self._recall_scopes_for_query(query)
        records_with_scope: list[tuple[FrameworkMemoryRecord, MemoryScope]] = []
        try:
            for recall_scope in recall_scopes:
                recall_request = FrameworkRecallRequest(
                    identity=identity,
                    query=query.query.strip() or "recent memories",
                    scope=recall_scope,
                    top_k=query.top_k,
                    memory_types=query.memory_types,
                    since=query.since,
                    max_tokens=max(64, min(8192, query.max_context_chars // 2)),
                )
                recalled = self._call("recall", lambda request=recall_request: self.adapter.recall(request))
                recalled = self._wait_for_mem0_recent_retain_visibility(
                    query=query,
                    identity=identity,
                    scope=recall_scope,
                    request=recall_request,
                    recalled=recalled,
                )
                records_with_scope.extend((record, recall_scope) for record in recalled.records)
        except Exception:
            return self._fallback_search(query)
        mappings_by_engine_id = self._mappings_by_engine_id_for_query(
            query=query,
            recall_scopes=recall_scopes,
        )
        deduped_records: list[tuple[FrameworkMemoryRecord, MemoryScope]] = []
        seen: set[str] = set()
        for record, recall_scope in records_with_scope:
            mapping = mappings_by_engine_id.get(record.engine_id)
            memory_id = record.project_memory_id or (mapping.project_memory_id if mapping else None) or record.engine_id
            if memory_id in seen:
                continue
            seen.add(memory_id)
            deduped_records.append((record, recall_scope))
        items = [
            item
            for record, recall_scope in deduped_records
            if (
                item := self._item_from_record(
                    record,
                    query,
                    mapping=mappings_by_engine_id.get(record.engine_id),
                    recall_scope=recall_scope,
                )
            )
            is not None
        ][: query.top_k]
        ranking_reason = f"framework_{self.adapter.name}_managed_recall"
        if not items and not records_with_scope:
            items = self._recent_retain_items_for_query(
                query=query,
                identity=identity,
                recall_scopes=recall_scopes,
            )
            if items:
                ranking_reason = "framework_mem0_recent_retain_consistency"
        return MemorySearchResult(
            items=items,
            query_used=query,
            total=len(items),
            ranking_reason=ranking_reason,
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
                item = self._item_from_record(
                    record,
                    query,
                    mapping=mapping,
                    recall_scope=mapping.scope or "project",
                )
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
                if _mapping_matches_identity_scope(mapping, identity)
            ]
        if not mappings:
            cancelled = self.ledger.cancel_pending_retain(
                user_id=user_id,
                project_memory_id=memory_id,
                identity=identity,
            )
            if cancelled:
                self._drop_recent_retain(user_id=user_id, project_memory_id=memory_id, identity=identity)
                self.ledger.record_tombstone(
                    user_id=user_id,
                    project_memory_id=memory_id,
                    engine_id="pending",
                )
                return True
            return False
        for mapping in mappings:
            self._drop_recent_retain(
                user_id=user_id,
                project_memory_id=memory_id,
                identity=mapping.identity,
                scope=mapping.scope,
            )
        for mapping in mappings:
            self.ledger.record_tombstone(
                user_id=user_id,
                project_memory_id=memory_id,
                engine_id=mapping.engine_id,
            )
        delete_mappings = (
            mappings[:1]
            if bool(getattr(self.adapter, "project_scoped_delete", False))
            else mappings
        )
        for mapping in delete_mappings:
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
        self._drop_recent_retain(user_id=user_id)

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
        scope = memory_scope_for_item(item)
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
            scope=scope,
            source=item.source,
            created_at=item.created_at,
            metadata={"tags": item.tags, "scope": scope} if item.tags or scope else {},
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
                scope=request.scope,
                project_memory_id=request.project_memory_id,
                engine_id=engine_id,
                engine_name=self.adapter.name,
                identity=request.identity,
            )
        self._mark_recent_retain(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            request=request,
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

    def _recall_scopes_for_query(self, query: MemoryQuery) -> list[MemoryScope]:
        allowed = set(query.allowed_scopes)
        unrestricted = not allowed
        scopes: list[MemoryScope] = []
        if query.session_id and (unrestricted or "session" in allowed):
            scopes.append("session")
        if unrestricted or allowed.intersection(_ENGINE_AGENT_SCOPES):
            scopes.append("project")
        if unrestricted or "user_profile" in allowed:
            scopes.append("user_profile")
        return scopes or ["project"]

    def _mappings_by_engine_id_for_query(
        self,
        *,
        query: MemoryQuery,
        recall_scopes: list[MemoryScope],
    ) -> dict[str, FrameworkMemoryMapping]:
        recall_scope_set = set(recall_scopes)
        mappings: dict[str, FrameworkMemoryMapping] = {}
        for mapping in self.ledger.list_mappings(user_id=query.user_id):
            if not _mapping_visible_for_query(mapping, query=query, recall_scopes=recall_scope_set):
                continue
            mappings[mapping.engine_id] = mapping
        return mappings

    def _mark_recent_retain(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        project_id: str | None,
        session_id: str | None,
        request: FrameworkRetainRequest,
    ) -> None:
        if self.adapter.name != "mem0":
            return
        key = self._recent_retain_key(user_id=user_id, identity=request.identity, scope=request.scope)
        now = time.monotonic()
        self._recent_retain_scopes[key] = now + _MEM0_RECALL_CONSISTENCY_TIMEOUT_SECONDS
        item = MemoryItem(
            memory_id=request.project_memory_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            scope=request.scope,
            memory_type=request.memory_type,
            summary=request.text,
            content={"framework": self.adapter.name, "_framework_recent_retain_consistency": True},
            source=request.source,
            created_at=request.created_at,
        )
        item_expires_at = now + _MEM0_RECENT_RETAIN_CACHE_SECONDS
        retained = self._recent_retain_items.setdefault(key, [])
        retained.append((item_expires_at, item))
        self._recent_retain_items[key] = retained[-10:]

    def _wait_for_mem0_recent_retain_visibility(
        self,
        *,
        query: MemoryQuery,
        identity,
        scope: MemoryScope,
        request: FrameworkRecallRequest,
        recalled: FrameworkRecallResult,
    ) -> FrameworkRecallResult:
        if recalled.records or not self._has_recent_retain(user_id=query.user_id, identity=identity, scope=scope):
            return recalled
        deadline = time.monotonic() + _MEM0_RECALL_CONSISTENCY_TIMEOUT_SECONDS
        current = recalled
        while time.monotonic() < deadline:
            time.sleep(_MEM0_RECALL_CONSISTENCY_POLL_SECONDS)
            current = self._call("recall", lambda: self.adapter.recall(request))
            if current.records:
                return current
        return current

    def _has_recent_retain(self, *, user_id: str, identity, scope: MemoryScope) -> bool:
        if self.adapter.name != "mem0":
            return False
        now = self._prune_recent_retains()
        return self._recent_retain_scopes.get(
            self._recent_retain_key(user_id=user_id, identity=identity, scope=scope),
            0,
        ) > now

    def _recent_retain_items_for_query(
        self,
        *,
        query: MemoryQuery,
        identity,
        recall_scopes: list[MemoryScope],
    ) -> list[MemoryItem]:
        if self.adapter.name != "mem0":
            return []
        now = self._prune_recent_retains()
        items: list[MemoryItem] = []
        for scope in recall_scopes:
            key = self._recent_retain_key(user_id=query.user_id, identity=identity, scope=scope)
            for expires_at, item in self._recent_retain_items.get(key, []):
                if expires_at <= now:
                    continue
                if query.memory_types and item.memory_type not in query.memory_types:
                    continue
                if query.since is not None and item.created_at < query.since:
                    continue
                if not _recent_item_visible_for_query(item, query=query, scope=scope):
                    continue
                if self.ledger.is_tombstoned(
                    user_id=query.user_id,
                    project_memory_id=item.memory_id,
                    engine_id=f"eng-{item.memory_id}",
                ):
                    continue
                items.append(item)
        return items[: query.top_k]

    def _drop_recent_retain(
        self,
        *,
        user_id: str,
        project_memory_id: str | None = None,
        identity=None,
        scope: MemoryScope | str | None = None,
    ) -> None:
        keys = list(self._recent_retain_items)
        for key in keys:
            if key[0] != user_id:
                continue
            if identity is not None and not self._recent_key_matches_identity(
                key,
                user_id=user_id,
                identity=identity,
                scope=scope,
            ):
                continue
            if project_memory_id is None:
                self._recent_retain_items.pop(key, None)
                self._recent_retain_scopes.pop(key, None)
                continue
            retained = [
                (expires_at, item)
                for expires_at, item in self._recent_retain_items.get(key, [])
                if item.memory_id != project_memory_id
            ]
            if retained:
                self._recent_retain_items[key] = retained
            else:
                self._recent_retain_items.pop(key, None)
                self._recent_retain_scopes.pop(key, None)

    def _prune_recent_retains(self) -> float:
        now = time.monotonic()
        expired = [key for key, expires_at in self._recent_retain_scopes.items() if expires_at <= now]
        for key in expired:
            self._recent_retain_scopes.pop(key, None)
        for key, retained in list(self._recent_retain_items.items()):
            current = [(expires_at, item) for expires_at, item in retained if expires_at > now]
            if current:
                self._recent_retain_items[key] = current
            else:
                self._recent_retain_items.pop(key, None)
        return now

    @staticmethod
    def _recent_retain_key(*, user_id: str, identity, scope: MemoryScope | str | None) -> tuple[str, str, str, str, str, str]:
        resolved_scope = _engine_scope_for_memory_scope(scope)
        mem0_agent_id = identity.agent_id if resolved_scope in {"session", "project"} else ""
        mem0_run_id = identity.run_id if resolved_scope == "session" else ""
        return (user_id, identity.user_id, mem0_agent_id, mem0_run_id, identity.tenant_tag, resolved_scope)

    def _recent_key_matches_identity(
        self,
        key: tuple[str, str, str, str, str, str],
        *,
        user_id: str,
        identity,
        scope: MemoryScope | str | None,
    ) -> bool:
        if scope is not None:
            return key == self._recent_retain_key(user_id=user_id, identity=identity, scope=scope)
        return key[0] == user_id and key[1] == identity.user_id and key[4] == identity.tenant_tag

    def _item_from_record(
        self,
        record: FrameworkMemoryRecord,
        query: MemoryQuery,
        *,
        mapping: FrameworkMemoryMapping | None,
        recall_scope: MemoryScope,
    ) -> MemoryItem | None:
        memory_id = record.project_memory_id or (mapping.project_memory_id if mapping else None) or record.engine_id
        if self.ledger.is_tombstoned(
            user_id=query.user_id,
            project_memory_id=memory_id,
            engine_id=record.engine_id,
        ):
            return None
        item_scope = mapping.scope if mapping and mapping.scope else recall_scope
        return MemoryItem(
            memory_id=memory_id,
            tenant_id=mapping.tenant_id if mapping else query.tenant_id,
            user_id=query.user_id,
            project_id=mapping.project_id if mapping else (query.project_id if recall_scope != "user_profile" else None),
            session_id=mapping.session_id if mapping else (query.session_id if recall_scope == "session" else None),
            scope=item_scope,
            memory_type=record.memory_type,
            summary=record.text,
            content={"framework": self.adapter.name, "engine_id": record.engine_id, "scope": item_scope},
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


def _engine_scope_for_memory_scope(scope: MemoryScope | str | None) -> str:
    if scope == "user_profile":
        return "user_profile"
    if scope in _ENGINE_AGENT_SCOPES:
        return "project"
    return "session"


def _mapping_matches_identity_scope(mapping: FrameworkMemoryMapping, identity) -> bool:
    if mapping.identity.user_id != identity.user_id or mapping.identity.tenant_tag != identity.tenant_tag:
        return False
    if mapping.scope is None:
        return mapping.identity.agent_id == identity.agent_id
    engine_scope = _engine_scope_for_memory_scope(mapping.scope)
    if engine_scope == "user_profile":
        return True
    if mapping.identity.agent_id != identity.agent_id:
        return False
    if engine_scope == "session" and mapping.identity.run_id != identity.run_id:
        return False
    return True


def _mapping_visible_for_query(
    mapping: FrameworkMemoryMapping,
    *,
    query: MemoryQuery,
    recall_scopes: set[MemoryScope],
) -> bool:
    if mapping.tenant_id is not None and mapping.tenant_id != query.tenant_id:
        return False
    scope = mapping.scope if mapping.scope in _VALID_ENGINE_SCOPES else None
    if scope is not None and query.allowed_scopes and scope not in query.allowed_scopes:
        return False
    engine_scope = _engine_scope_for_memory_scope(scope) if scope is not None else "project"
    if engine_scope not in {_engine_scope_for_memory_scope(recall_scope) for recall_scope in recall_scopes}:
        return False
    if engine_scope == "user_profile":
        return True
    if mapping.project_id is not None and mapping.project_id != query.project_id:
        return False
    if engine_scope == "session" and mapping.session_id is not None and mapping.session_id != query.session_id:
        return False
    return True


def _recent_item_visible_for_query(item: MemoryItem, *, query: MemoryQuery, scope: MemoryScope) -> bool:
    if item.tenant_id is not None and item.tenant_id != query.tenant_id:
        return False
    engine_scope = _engine_scope_for_memory_scope(scope)
    if engine_scope == "user_profile":
        return True
    if item.project_id is not None and item.project_id != query.project_id:
        return False
    if engine_scope == "session" and item.session_id != query.session_id:
        return False
    return True


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
