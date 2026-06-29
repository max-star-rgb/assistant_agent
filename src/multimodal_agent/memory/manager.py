"""Memory manager boundary for layered agent memory access."""

from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, Field

from multimodal_agent.memory.context_builder import MemoryContextBlock, MemoryContextBuilder, MemoryLayer
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID, UserProfileMemory
from multimodal_agent.memory.store import MemoryStore
from multimodal_agent.memory.write_policy import (
    MemoryWritePolicy,
    build_explicit_memory_item,
    build_memory_item_from_promotion_candidate,
    build_run_summary_promotion_candidate,
    promotion_decision_audit_record,
)
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import (
    MemoryItem,
    MemoryQuery,
    MemoryScope,
    MemorySearchResult,
    memory_item_matches_query_scope,
    memory_scope_for_item,
)
from multimodal_agent.schemas.memory_audit import (
    MemoryAuditEvent,
    MemoryAuditEventOutcome,
    MemoryAuditEventType,
    MemoryProfileRepairAction,
    MemoryProfileRepairResult,
)
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


_VALID_MEMORY_SCOPES = {"session", "task", "project", "user_profile", "video", "product"}


class MemoryContext(BaseModel):
    """Structured memory context loaded for one agent run."""

    items: list[MemoryItem] = Field(default_factory=list)
    text: str = ""
    summaries: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    blocks: list[MemoryContextBlock] = Field(default_factory=list)
    total_tokens: int = Field(default=0, ge=0)
    budget_tokens: int = Field(default=0, ge=0)
    omitted_count: int = Field(default=0, ge=0)
    rejected_reasons: list[str] = Field(default_factory=list)
    retrieval_version: str = ""


class MemoryManager:
    """Coordinate memory retrieval, write policy, and context formatting.

    Agent nodes and tools should depend on this boundary instead of reaching
    directly into stores, retrievers, and context builders.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        write_policy: MemoryWritePolicy | None = None,
        default_top_k: int = 5,
        default_max_context_chars: int = 500,
        default_max_context_tokens: int | None = None,
        context_builder: MemoryContextBuilder | None = None,
        max_audit_events: int = 1000,
    ) -> None:
        self.store = store
        self.write_policy = write_policy or MemoryWritePolicy()
        self.default_top_k = default_top_k
        self.default_max_context_chars = default_max_context_chars
        self.default_max_context_tokens = default_max_context_tokens
        self.context_builder = context_builder or MemoryContextBuilder()
        self.max_audit_events = max(1, max_audit_events)
        self._audit_events: list[MemoryAuditEvent] = []

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        """Search through the configured store."""

        return self.store.search(query)

    def search_for_identity(self, identity: RequestIdentity, query: MemoryQuery) -> MemorySearchResult:
        """Search memory for an identity, ignoring caller-supplied user_id."""

        scoped_query = _query_for_identity(identity, query)
        return self.search(scoped_query)

    def load_context_for_request(
        self,
        request: UserRequest,
        *,
        capability: str | None = None,
        top_k: int | None = None,
        max_context_chars: int | None = None,
        max_context_tokens: int | None = None,
    ) -> MemoryContext:
        """Load bounded, layered memory context for a user request."""

        return self.load_context_for_identity(
            RequestIdentity.from_user_request(request),
            query_text=request.text or "",
            capability=capability,
            top_k=top_k,
            max_context_chars=max_context_chars,
            max_context_tokens=max_context_tokens or _memory_context_token_budget_from_metadata(request.metadata),
        )

    def load_context_for_identity(
        self,
        identity: RequestIdentity,
        *,
        query_text: str = "",
        capability: str | None = None,
        top_k: int | None = None,
        max_context_chars: int | None = None,
        max_context_tokens: int | None = None,
    ) -> MemoryContext:
        """Load bounded, layered memory context for an identity."""

        query = MemoryQuery(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            query=query_text,
            capability=capability,
            allowed_scopes=_allowed_memory_scopes(identity),
            top_k=top_k or self.default_top_k,
            max_context_chars=max_context_chars or self.default_max_context_chars,
        )
        result = self.search_for_identity(identity, query)
        context = self.build_context(
            result.items,
            max_chars=query.max_context_chars,
            max_tokens=max_context_tokens or self.default_max_context_tokens,
        )
        self.record_audit_event(
            "memory_context_loaded",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=identity.session_id,
            summary="memory context loaded",
            counts={
                "retrieved": result.total,
                "injected": len(context.items),
                "tokens": context.total_tokens,
                "budget_tokens": context.budget_tokens,
                "omitted": context.omitted_count,
                "rejected": len(context.rejected_reasons),
            },
            metadata={
                "capability": capability,
                "query_present": bool(query_text.strip()),
                "retrieval_version": context.retrieval_version,
                "injected_memory_ids": [item.memory_id for item in context.items],
                "rejected_reasons": context.rejected_reasons[:8],
            },
        )
        return context

    def load_into_state(
        self,
        state: Any,
        request: UserRequest,
        *,
        capability: str | None = None,
        top_k: int | None = None,
        max_context_chars: int | None = None,
        max_context_tokens: int | None = None,
    ) -> MemoryContext:
        """Load memory and attach prompt-safe metadata to AgentState."""

        context = self.load_context_for_request(
            request,
            capability=capability,
            top_k=top_k,
            max_context_chars=max_context_chars,
            max_context_tokens=max_context_tokens,
        )
        state.memory_context = context.items
        state.request.metadata["memory_context_text"] = context.text
        state.request.metadata["memory_context_summaries"] = context.summaries
        state.request.metadata["memory_context_refs"] = context.artifact_refs
        state.request.metadata["memory_context_blocks"] = [
            block.model_dump(mode="json") for block in context.blocks
        ]
        state.request.metadata["memory_context_tokens"] = context.total_tokens
        state.request.metadata["memory_context_budget_tokens"] = context.budget_tokens
        state.request.metadata["memory_context_omitted_count"] = context.omitted_count
        state.request.metadata["memory_context_rejected_reasons"] = context.rejected_reasons
        state.request.metadata["memory_context_retrieval_version"] = context.retrieval_version
        state.request.metadata["memory_context_injected_ids"] = [item.memory_id for item in context.items]
        return context

    def build_context(
        self,
        items: list[MemoryItem],
        *,
        max_chars: int | None = None,
        max_tokens: int | None = None,
    ) -> MemoryContext:
        """Build grouped context text while preserving the retrieved items."""

        pack = self.context_builder.build(
            items,
            max_chars=max_chars or self.default_max_context_chars,
            budget_tokens=max_tokens or self.default_max_context_tokens,
        )
        summaries = [item.summary for item in pack.items]
        artifact_refs = [ref for item in pack.items for ref in item.artifact_refs]
        return MemoryContext(
            items=pack.items,
            text=pack.rendered_context,
            summaries=summaries,
            artifact_refs=artifact_refs,
            blocks=pack.blocks,
            total_tokens=pack.total_tokens,
            budget_tokens=pack.budget_tokens,
            omitted_count=pack.omitted_count,
            rejected_reasons=pack.rejected_reasons,
            retrieval_version=pack.retrieval_version,
        )

    def save_from_run(self, state: Any) -> MemoryItem | None:
        """Evaluate a completed-run memory candidate and persist only when policy allows."""

        if state.status != "completed" or state.response is None:
            return None
        if _is_pure_memory_save_run(state):
            return None
        output_refs = [
            ref
            for result in state.tool_results
            for ref in ([result.output_ref] if result.output_ref else [])
        ]
        candidate = build_run_summary_promotion_candidate(
            user_id=state.user_id,
            session_id=state.session_id,
            summary=state.response.message if state.response else "Agent run completed.",
            intent=state.intent.intent if state.intent else None,
            selected_tools=[tool.tool_name for tool in state.selected_tools],
            output_refs=output_refs,
            policy=self.write_policy,
        )
        if candidate is None:
            _record_no_promotion_candidate(state, "auto_save_task_summary_disabled")
            self.record_audit_event(
                "memory_promotion_decided",
                user_id=state.user_id,
                session_id=state.session_id,
                outcome="skipped",
                summary="promotion candidate skipped",
                counts={"candidates": 0, "written": 0, "rejected": 0},
                metadata={"reason": "auto_save_task_summary_disabled"},
            )
            return None
        decision = self.write_policy.evaluate_promotion_candidate(candidate)
        item = build_memory_item_from_promotion_candidate(
            memory_id=f"run_memory_{uuid4().hex}",
            candidate=candidate,
            policy=self.write_policy,
            created_at=datetime.now(timezone.utc),
        )
        saved = self.store.save(item) if item is not None else None
        _record_promotion_decision(state, decision, saved)
        self.record_audit_event(
            "memory_promotion_decided",
            user_id=candidate.user_id,
            session_id=candidate.session_id,
            memory_id=saved.memory_id if saved is not None else None,
            outcome="succeeded" if saved is not None else "rejected",
            summary="promotion candidate written" if saved is not None else "promotion candidate rejected",
            counts={
                "candidates": 1,
                "written": 1 if saved is not None else 0,
                "rejected": 0 if saved is not None else 1,
            },
            metadata=promotion_decision_audit_record(
                decision,
                written_memory_id=saved.memory_id if saved is not None else None,
            ),
        )
        return saved

    def save_explicit(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        content: dict[str, Any] | None = None,
        memory_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        scope: MemoryScope | None = None,
        created_at: datetime | None = None,
    ) -> MemoryItem:
        """Persist an explicit user-requested memory."""

        decision = self.write_policy.evaluate_explicit_save(text=text, content=content, scope=scope)
        if not decision.allowed:
            self.record_audit_event(
                "memory_explicit_saved",
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                outcome="rejected",
                summary="explicit memory rejected",
                counts={"attempted": 1, "written": 0, "rejected": 1},
                metadata=promotion_decision_audit_record(decision),
            )
            raise ValueError(decision.reason)
        item = build_explicit_memory_item(
            memory_id=memory_id or f"explicit_memory_{uuid4().hex}",
            user_id=user_id,
            session_id=session_id,
            text=text,
            content=content,
            scope=scope,
            policy=self.write_policy,
            created_at=created_at,
        )
        if tenant_id is not None or project_id is not None or scope is not None:
            item = item.model_copy(
                update={
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "scope": scope or item.scope,
                }
            )
        saved = self._merge_or_save(item)
        self._upsert_user_profile(saved)
        self.record_audit_event(
            "memory_explicit_saved",
            user_id=saved.user_id,
            tenant_id=saved.tenant_id,
            project_id=saved.project_id,
            session_id=saved.session_id,
            memory_id=saved.memory_id,
            summary="explicit memory saved",
            counts={"attempted": 1, "written": 1, "rejected": 0},
            metadata={
                "memory_type": saved.memory_type,
                "scope": memory_scope_for_item(saved),
                "sensitivity": saved.sensitivity,
                "destination": decision.destination,
                "ttl_days": decision.ttl_days,
            },
        )
        return saved

    def save_explicit_for_identity(
        self,
        identity: RequestIdentity,
        *,
        text: str,
        content: dict[str, Any] | None = None,
        memory_id: str | None = None,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        created_at: datetime | None = None,
    ) -> MemoryItem:
        """Persist an explicit memory for an identity, ignoring caller user_id."""

        resolved_session_id = session_id or identity.session_id
        if not resolved_session_id:
            raise ValueError("session_id is required to save memory for identity")
        return self.save_explicit(
            user_id=identity.user_id,
            session_id=resolved_session_id,
            text=text,
            content=content,
            memory_id=memory_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            scope=scope,
            created_at=created_at,
        )

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        return self.get_for_identity(RequestIdentity.for_user(user_id=user_id), memory_id)

    def get_for_identity(self, identity: RequestIdentity, memory_id: str) -> MemoryItem | None:
        item = self.store.get(identity.user_id, memory_id)
        if item is None or not _identity_allows_item(identity, item):
            return None
        return item

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        return self.list_for_identity(RequestIdentity.for_user(user_id=user_id))

    def list_for_identity(self, identity: RequestIdentity) -> list[MemoryItem]:
        return [item for item in self.store.list_by_user(identity.user_id) if _identity_allows_item(identity, item)]

    def delete(self, user_id: str, memory_id: str) -> bool:
        return self.delete_for_identity(RequestIdentity.for_user(user_id=user_id), memory_id)

    def delete_for_identity(self, identity: RequestIdentity, memory_id: str) -> bool:
        item = self.get_for_identity(identity, memory_id)
        if item is None:
            return False
        deleted = self.store.delete(identity.user_id, memory_id)
        if deleted:
            self.record_audit_event(
                "memory_deleted",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=item.session_id,
                memory_id=memory_id,
                summary="memory soft-deleted",
                counts={"memory_items": 1},
                metadata={"memory_type": item.memory_type, "scope": memory_scope_for_item(item)},
            )
        return deleted

    def hard_delete(self, user_id: str, memory_id: str) -> bool:
        return self.hard_delete_for_identity(RequestIdentity.for_user(user_id=user_id), memory_id)

    def hard_delete_for_identity(self, identity: RequestIdentity, memory_id: str) -> bool:
        item = self.get_for_identity(identity, memory_id)
        if item is None:
            return False
        hard_delete = getattr(self.store, "hard_delete", None)
        if callable(hard_delete):
            deleted = bool(hard_delete(identity.user_id, memory_id))
        else:
            deleted = self.store.delete(identity.user_id, memory_id)
        if deleted:
            self.record_audit_event(
                "memory_hard_deleted",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=item.session_id,
                memory_id=memory_id,
                summary="memory hard-deleted",
                counts={"memory_items": 1},
                metadata={"memory_type": item.memory_type, "scope": memory_scope_for_item(item)},
            )
        return deleted

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        return self.delete_session_for_identity(
            RequestIdentity.for_user(user_id=user_id, session_id=session_id),
            session_id=session_id,
        )

    def delete_session_for_identity(self, identity: RequestIdentity, *, session_id: str | None = None) -> int:
        resolved_session_id = session_id or identity.session_id
        if not resolved_session_id:
            raise ValueError("session_id is required to delete session memories for identity")
        items = [
            item
            for item in self.list_for_identity(identity)
            if (item.session_id or item.content.get("session_id")) == resolved_session_id
        ]
        deleted = 0
        for item in items:
            if self.store.delete(identity.user_id, item.memory_id):
                deleted += 1
        self.record_audit_event(
            "memory_session_deleted",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=resolved_session_id,
            summary="session memory deleted",
            counts={"memory_items": deleted},
        )
        return deleted

    def clear_user(self, user_id: str) -> None:
        self.clear_identity(RequestIdentity.for_user(user_id=user_id))

    def clear_identity(self, identity: RequestIdentity) -> None:
        deleted = 0
        for item in self.list_for_identity(identity):
            if self.store.delete(identity.user_id, item.memory_id):
                deleted += 1
        self.record_audit_event(
            "memory_user_cleared",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=identity.session_id,
            summary="user memory cleared",
            counts={"memory_items": deleted},
        )

    def rebuild_user_profile_for_identity(
        self,
        identity: RequestIdentity,
        *,
        dry_run: bool = False,
        record_event: bool = True,
    ) -> MemoryProfileRepairResult:
        """Rebuild the compact global user profile from source memories."""

        now = datetime.now(timezone.utc)
        existing = self.get_for_identity(identity, USER_PROFILE_MEMORY_ID)
        source_items = _profile_source_items(self.list_for_identity(identity))
        expected_profile = UserProfileMemory.empty(identity.user_id, now=now)
        for item in sorted(source_items, key=lambda item: item.created_at):
            expected_profile.merge_memory(item, now=item.updated_at or item.created_at)
        expected_item = expected_profile.to_memory_item(session_id=existing.session_id if existing else identity.session_id)
        if existing is not None:
            expected_item = expected_item.model_copy(update={"created_at": existing.created_at})

        current_source_ids = _profile_source_ids(existing)
        expected_source_ids = list(expected_profile.source_memory_ids)
        missing_source_ids = [memory_id for memory_id in expected_source_ids if memory_id not in current_source_ids]
        stale_source_ids = [memory_id for memory_id in current_source_ids if memory_id not in expected_source_ids]
        issues = _profile_repair_issues(
            existing=existing,
            expected_item=expected_item,
            source_count=len(source_items),
            missing_source_ids=missing_source_ids,
            stale_source_ids=stale_source_ids,
        )
        action = _profile_repair_action(existing=existing, source_count=len(source_items), issues=issues)
        repaired = False
        if not dry_run and action != "none":
            if action == "delete":
                repaired = self.store.delete(identity.user_id, USER_PROFILE_MEMORY_ID)
            else:
                self.store.save(expected_item)
                repaired = True
        profile_present_after = (
            existing is not None if dry_run else self.get_for_identity(identity, USER_PROFILE_MEMORY_ID) is not None
        )
        result = MemoryProfileRepairResult(
            user_id=identity.user_id,
            dry_run=dry_run,
            repaired=repaired,
            action=action,
            profile_memory_id=USER_PROFILE_MEMORY_ID if (existing is not None or source_items) else None,
            profile_present_before=existing is not None,
            profile_present_after=profile_present_after,
            source_count=len(source_items),
            expected_source_memory_ids=expected_source_ids,
            current_source_memory_ids=current_source_ids,
            missing_source_memory_ids=missing_source_ids,
            stale_source_memory_ids=stale_source_ids,
            issues=issues,
            expected_summary=expected_item.summary if source_items else None,
            current_summary=existing.summary if existing is not None else None,
        )
        if record_event:
            self.record_audit_event(
                "memory_profile_repaired",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=identity.session_id,
                memory_id=USER_PROFILE_MEMORY_ID,
                outcome="skipped" if dry_run or action == "none" else "succeeded",
                summary="user profile repair checked" if dry_run else "user profile repair applied",
                counts={
                    "source_items": result.source_count,
                    "issues": len(result.issues),
                    "repaired": 1 if result.repaired else 0,
                },
                metadata={
                    "action": result.action,
                    "issues": result.issues,
                    "missing_source_memory_ids": result.missing_source_memory_ids[:50],
                    "stale_source_memory_ids": result.stale_source_memory_ids[:50],
                },
            )
        return result

    def record_audit_event(
        self,
        event_type: MemoryAuditEventType,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        memory_id: str | None = None,
        outcome: MemoryAuditEventOutcome = "succeeded",
        summary: str = "",
        counts: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryAuditEvent:
        """Record a prompt-safe, local memory audit event."""

        event = MemoryAuditEvent(
            event_id=f"memory_event_{uuid4().hex}",
            event_type=event_type,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            memory_id=memory_id,
            occurred_at=datetime.now(timezone.utc),
            outcome=outcome,
            summary=sanitize_error_message(summary or event_type),
            counts=_audit_counts(counts or {}),
            metadata=_audit_metadata(metadata or {}),
        )
        save_audit_event = getattr(self.store, "save_audit_event", None)
        if callable(save_audit_event):
            save_audit_event(event)
        self._audit_events.append(event)
        del self._audit_events[:-self.max_audit_events]
        return event

    def list_audit_events_for_identity(
        self,
        identity: RequestIdentity,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[MemoryAuditEvent]:
        """List recent memory audit events visible to an identity."""

        resolved_limit = max(1, min(limit, self.max_audit_events))
        list_audit_events = getattr(self.store, "list_audit_events", None)
        if callable(list_audit_events):
            return list_audit_events(
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                event_type=event_type,
                limit=resolved_limit,
            )
        events = [
            event
            for event in self._audit_events
            if _identity_allows_event(identity, event)
            and (event_type is None or event.event_type == event_type)
        ]
        return list(reversed(events[-resolved_limit:]))

    def _merge_or_save(self, item: MemoryItem) -> MemoryItem:
        duplicate = self._find_duplicate(item)
        if duplicate is None:
            return self.store.save(item)

        observation_count = _observation_count(duplicate) + 1
        merged = duplicate.model_copy(
            update={
                "session_id": duplicate.session_id or item.session_id,
                "content": {
                    **duplicate.content,
                    **item.content,
                    "observation_count": observation_count,
                },
                "tags": _unique([*duplicate.tags, *item.tags]),
                "artifact_refs": _unique([*duplicate.artifact_refs, *item.artifact_refs]),
                "updated_at": item.created_at,
                "sensitivity": _merged_sensitivity(duplicate.sensitivity, item.sensitivity),
            }
        )
        return self.store.save(merged)

    def _find_duplicate(self, item: MemoryItem) -> MemoryItem | None:
        item_key = _dedupe_key(item)
        if not item_key:
            return None
        for existing in self.store.list_by_user(item.user_id):
            if existing.memory_id == item.memory_id or existing.source == "user_profile":
                continue
            if existing.memory_type != item.memory_type:
                continue
            if not _same_governance_scope(existing, item):
                continue
            if _dedupe_key(existing) == item_key:
                return existing
        return None

    def _upsert_user_profile(self, item: MemoryItem) -> MemoryItem | None:
        if item.source == "user_profile" or item.memory_type not in {"preference", "product", "task"}:
            return None
        if item.tenant_id is not None or item.project_id is not None or memory_scope_for_item(item) == "project":
            return None

        existing = self.store.get(item.user_id, USER_PROFILE_MEMORY_ID)
        profile = (
            UserProfileMemory.from_memory_item(existing)
            if existing is not None
            else UserProfileMemory.empty(item.user_id, now=item.created_at)
        )
        changed = profile.merge_memory(item, now=item.updated_at or item.created_at)
        if not changed and existing is not None:
            return existing

        profile_item = profile.to_memory_item(session_id=item.session_id)
        if existing is not None:
            profile_item = profile_item.model_copy(update={"created_at": existing.created_at})
        return self.store.save(profile_item)


def _query_for_identity(identity: RequestIdentity, query: MemoryQuery) -> MemoryQuery:
    return query.model_copy(
        update={
            "user_id": identity.user_id,
            "tenant_id": identity.tenant_id,
            "project_id": identity.project_id,
            "allowed_scopes": _allowed_memory_scopes(identity),
        }
    )


def _identity_allows_item(identity: RequestIdentity, item: MemoryItem) -> bool:
    query = MemoryQuery(
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        allowed_scopes=_allowed_memory_scopes(identity),
    )
    return memory_item_matches_query_scope(item, query)


def _identity_allows_event(identity: RequestIdentity, event: MemoryAuditEvent) -> bool:
    if event.user_id != identity.user_id:
        return False
    if event.tenant_id is not None and event.tenant_id != identity.tenant_id:
        return False
    if event.project_id is not None and event.project_id != identity.project_id:
        return False
    return True


def _profile_source_items(items: list[MemoryItem]) -> list[MemoryItem]:
    return [
        item
        for item in items
        if item.source != "user_profile"
        and item.memory_type in {"preference", "product", "task"}
        and item.tenant_id is None
        and item.project_id is None
        and memory_scope_for_item(item) != "project"
        and not _is_expired(item)
    ]


def _profile_source_ids(item: MemoryItem | None) -> list[str]:
    if item is None:
        return []
    value = item.content.get("source_memory_ids")
    if not isinstance(value, list):
        return []
    return [str(memory_id) for memory_id in value if str(memory_id).strip()]


def _profile_repair_issues(
    *,
    existing: MemoryItem | None,
    expected_item: MemoryItem,
    source_count: int,
    missing_source_ids: list[str],
    stale_source_ids: list[str],
) -> list[str]:
    issues: list[str] = []
    if existing is None and source_count > 0:
        issues.append("profile_missing")
    if existing is not None and source_count == 0:
        issues.append("profile_orphaned")
    if existing is not None and source_count > 0:
        if missing_source_ids:
            issues.append("profile_missing_sources")
        if stale_source_ids:
            issues.append("profile_stale_sources")
        if existing.summary != expected_item.summary:
            issues.append("profile_summary_out_of_sync")
        if existing.content.get("preferences") != expected_item.content.get("preferences"):
            issues.append("profile_preferences_out_of_sync")
        if existing.content.get("facts") != expected_item.content.get("facts"):
            issues.append("profile_facts_out_of_sync")
    return list(dict.fromkeys(issues))


def _profile_repair_action(
    *,
    existing: MemoryItem | None,
    source_count: int,
    issues: list[str],
) -> MemoryProfileRepairAction:
    if not issues:
        return "none"
    if existing is None and source_count > 0:
        return "create"
    if existing is not None and source_count == 0:
        return "delete"
    return "update"


def _same_governance_scope(left: MemoryItem, right: MemoryItem) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.project_id == right.project_id
        and memory_scope_for_item(left) == memory_scope_for_item(right)
    )


def _allowed_memory_scopes(identity: RequestIdentity) -> list[MemoryScope]:
    return [
        cast(MemoryScope, scope)
        for scope in identity.allowed_scopes
        if scope in _VALID_MEMORY_SCOPES
    ]


def _memory_context_token_budget_from_metadata(metadata: dict[str, Any]) -> int | None:
    for key in ("memory_context_max_tokens", "memory_context_budget_tokens"):
        value = metadata.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _is_expired(item: MemoryItem) -> bool:
    if item.expires_at is None:
        return False
    now = datetime.now(tz=item.expires_at.tzinfo or timezone.utc)
    return item.expires_at < now


def _is_pure_memory_save_run(state: Any) -> bool:
    successful_tool_names = {
        result.tool_name
        for result in getattr(state, "tool_results", [])
        if getattr(result, "success", False)
    }
    return successful_tool_names == {"memory_save"}


def _record_no_promotion_candidate(state: Any, reason: str) -> None:
    metadata = getattr(getattr(state, "request", None), "metadata", None)
    if not isinstance(metadata, dict):
        return
    metadata["auto_task_summary_memory"] = {
        "skipped": True,
        "reason": reason,
        "candidate": False,
    }


def _record_promotion_decision(state: Any, decision: Any, saved: MemoryItem | None) -> None:
    metadata = getattr(getattr(state, "request", None), "metadata", None)
    if not isinstance(metadata, dict):
        return
    _increment_metadata_count(metadata, "memory_promotion_candidates")
    if saved is not None:
        _increment_metadata_count(metadata, "memory_promotion_written")
    else:
        _increment_metadata_count(metadata, "memory_promotion_rejected")
    audit = metadata.setdefault("memory_promotion_candidate_audit", [])
    if isinstance(audit, list):
        audit.append(promotion_decision_audit_record(decision, written_memory_id=saved.memory_id if saved else None))
        del audit[:-10]
    metadata["auto_task_summary_memory"] = {
        "skipped": saved is None,
        "reason": decision.reason,
        "candidate": True,
        "written": saved is not None,
        "memory_id": saved.memory_id if saved else None,
    }


def _increment_metadata_count(metadata: dict[str, Any], key: str) -> None:
    value = metadata.get(key)
    metadata[key] = value + 1 if isinstance(value, int) and value >= 0 else 1


def _audit_counts(counts: dict[str, int]) -> dict[str, int]:
    clean: dict[str, int] = {}
    for key, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        clean[sanitize_error_message(key)] = value
    return clean


def _audit_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_error_detail(metadata)
    return sanitized if isinstance(sanitized, dict) else {}


def _dedupe_key(item: MemoryItem) -> str:
    return _normalize_for_dedupe(item.summary)


def _normalize_for_dedupe(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _observation_count(item: MemoryItem) -> int:
    value = item.content.get("observation_count")
    if isinstance(value, int) and value >= 1:
        return value
    return 1


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _merged_sensitivity(left: str, right: str) -> str:
    order = {"normal": 0, "private": 1, "sensitive": 2}
    return left if order.get(left, 0) >= order.get(right, 0) else right
