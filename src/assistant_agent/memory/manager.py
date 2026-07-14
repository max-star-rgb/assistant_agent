"""Memory manager boundary for layered agent memory access."""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, Field

from assistant_agent.memory.context_builder import MemoryContextBlock, MemoryContextBuilder
from assistant_agent.memory.conflict_resolver import MemoryConflictResolver
from assistant_agent.memory.facts import (
    fact_content,
    fact_from_item,
    is_active_memory_fact,
    mark_fact_superseded,
    memory_fact_status,
)
from assistant_agent.memory.profile import USER_PROFILE_MEMORY_ID, UserProfileMemory
from assistant_agent.memory.read_policy import (
    MemoryReadDecision,
    MemoryReadPolicy,
    memory_usage_hint,
    trust_policy_metadata,
)
from assistant_agent.memory.remote import MemoryServiceOperationError
from assistant_agent.memory.store import MemoryStore
from assistant_agent.memory.write_policy import (
    MemoryWritePolicy,
    MemorySaveSourceIntent,
    build_explicit_memory_item,
    build_memory_item_from_promotion_candidate,
    build_run_summary_promotion_candidate,
    promotion_decision_audit_record,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import (
    MemoryItem,
    MemoryQuery,
    MemoryScope,
    MemorySearchResult,
    MemoryType,
    memory_item_matches_query_scope,
    memory_scope_for_item,
)
from assistant_agent.schemas.memory_audit import (
    MemoryAuditEvent,
    MemoryAuditEventOutcome,
    MemoryAuditEventType,
    MemoryConfirmationStatus,
    MemoryPendingConfirmation,
    MemoryProfileRepairAction,
    MemoryProfileRepairResult,
)
from assistant_agent.schemas.memory_intelligence import MemoryConflictDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


_VALID_MEMORY_SCOPES = {"session", "task", "project", "user_profile", "video", "product"}
_VALID_MEMORY_TYPES = {
    "conversation",
    "video",
    "image",
    "product",
    "preference",
    "artifact",
    "task",
    "generation",
    "render",
}
_SAFE_EXPLICIT_CONTENT_KEYS = {
    "summary",
    "preference_key",
    "style",
    "budget",
    "product_ref",
    "product_id",
    "item",
    "output_ref",
    "fact",
    "fact_key",
    "fact_value",
    "subject",
    "predicate",
    "conflict_policy",
    "confidence",
}


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
    read_policy_allowed: bool = True
    read_policy_reason: str = ""
    read_policy: dict[str, Any] = Field(default_factory=dict)
    trust_policy: dict[str, Any] = Field(default_factory=trust_policy_metadata)
    usage_hint: str = Field(default_factory=memory_usage_hint)
    recall_report: dict[str, Any] = Field(default_factory=dict)


class MemoryConfirmationRequired(ValueError):
    """Raised when an explicit memory save requires user confirmation."""

    def __init__(self, confirmation: MemoryPendingConfirmation) -> None:
        super().__init__(confirmation.reason)
        self.confirmation = confirmation


class _MemoryFactConflictRequired(ValueError):
    """Internal signal carrying a pure conflict decision to the save boundary."""

    def __init__(self, item: MemoryItem, decision: MemoryConflictDecision) -> None:
        super().__init__(decision.reason)
        self.item = item
        self.decision = decision


class MemorySaveCandidateResult(BaseModel):
    """Audit-only result for a memory_save assistant candidate."""

    candidate_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_intent: MemorySaveSourceIntent = "assistant_candidate"
    summary: str = ""
    memory_type: str = ""
    source_reason: str = ""
    future_use: str = ""
    evidence: str = ""
    written: bool = False
    reason: str = ""


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
        read_policy: MemoryReadPolicy | None = None,
        max_audit_events: int = 1000,
        default_confirmation_ttl_seconds: int = 86400,
    ) -> None:
        self.store = store
        self.write_policy = write_policy or MemoryWritePolicy()
        self.read_policy = read_policy or MemoryReadPolicy()
        self.default_top_k = default_top_k
        self.default_max_context_chars = default_max_context_chars
        self.default_max_context_tokens = default_max_context_tokens
        self.context_builder = context_builder or MemoryContextBuilder()
        self.conflict_resolver = MemoryConflictResolver()
        self.max_audit_events = max(1, max_audit_events)
        self.default_confirmation_ttl_seconds = max(60, default_confirmation_ttl_seconds)
        self._audit_events: list[MemoryAuditEvent] = []
        self._pending_confirmations: dict[str, MemoryPendingConfirmation] = {}

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        """Search through the configured store."""

        return self.store.search(query)

    def search_for_identity(self, identity: RequestIdentity, query: MemoryQuery) -> MemorySearchResult:
        """Search memory for an identity, ignoring caller-supplied user_id."""

        scoped_query = _query_for_identity(identity, query)
        result = self.search(scoped_query)
        self._record_remote_search_event(identity=identity, result=result)
        return result

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

        token_budget = max_context_tokens or _memory_context_token_budget_from_metadata(request.metadata)
        decision = self.read_policy.decide_auto_load(
            request_text=request.text or "",
            metadata=request.metadata,
            top_k=top_k or self.default_top_k,
            max_context_chars=max_context_chars or self.default_max_context_chars,
            max_context_tokens=token_budget,
        )
        if not decision.allowed:
            context = _skipped_memory_context(decision)
            identity = RequestIdentity.from_user_request(request)
            self.record_audit_event(
                "memory_context_loaded",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=identity.session_id,
                outcome="skipped",
                summary="memory context skipped by read policy",
                counts={
                    "retrieved": 0,
                    "injected": 0,
                    "tokens": 0,
                    "budget_tokens": context.budget_tokens,
                },
                metadata={"read_policy": decision.prompt_safe_metadata()},
            )
            return context
        return self.load_context_for_identity(
            RequestIdentity.from_user_request(request),
            query_text=request.text or "",
            capability=capability,
            top_k=decision.top_k,
            max_context_chars=decision.max_context_chars,
            max_context_tokens=decision.max_context_tokens,
            read_decision=decision,
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
        read_decision: MemoryReadDecision | None = None,
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
        recall_report = _memory_recall_report(
            identity=identity,
            query_text=query_text,
            result=result,
            context=context,
            read_decision=read_decision,
            profile_item=self.get_for_identity(identity, USER_PROFILE_MEMORY_ID),
            visible_items=self.list_for_identity(identity),
        )
        if read_decision is not None:
            context = context.model_copy(
                update={
                    "read_policy_allowed": read_decision.allowed,
                    "read_policy_reason": read_decision.reason,
                    "read_policy": read_decision.prompt_safe_metadata(),
                    "trust_policy": read_decision.trust_policy,
                    "usage_hint": read_decision.usage_hint,
                    "recall_report": recall_report,
                },
                deep=True,
            )
        else:
            context = context.model_copy(update={"recall_report": recall_report}, deep=True)
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
                "recall_report": context.recall_report,
                "read_policy": (
                    read_decision.prompt_safe_metadata() if read_decision is not None else {}
                ),
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
        state.request.metadata["memory_context_skipped"] = context.read_policy_allowed is False
        state.request.metadata["memory_context_policy_reason"] = context.read_policy_reason
        state.request.metadata["memory_read_policy"] = context.read_policy
        state.request.metadata["memory_trust_policy"] = context.trust_policy
        state.request.metadata["memory_usage_hint"] = context.usage_hint
        state.request.metadata["memory_recall_report"] = context.recall_report
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
        source_intent: MemorySaveSourceIntent | None = None,
        source_reason: str | None = None,
        future_use: str | None = None,
        evidence: str | None = None,
        created_at: datetime | None = None,
    ) -> MemoryItem | MemorySaveCandidateResult:
        """Persist explicit memory or record an assistant memory candidate."""

        resolved_source_intent = source_intent or "user_explicit"
        if resolved_source_intent not in {"user_explicit", "assistant_candidate", "user_confirmed"}:
            reason = "memory save source_intent is invalid"
            self.record_audit_event(
                "memory_explicit_saved",
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                outcome="rejected",
                summary="memory save source rejected",
                counts={"attempted": 1, "written": 0, "rejected": 1},
                metadata={"source_intent": str(resolved_source_intent), "reason": reason},
            )
            raise ValueError(reason)
        source_metadata = _memory_save_source_metadata(
            source_intent=cast(MemorySaveSourceIntent, resolved_source_intent),
            source_reason=source_reason,
            future_use=future_use,
            evidence=evidence,
        )
        if resolved_source_intent == "user_confirmed":
            reason = "source_intent=user_confirmed is reserved for confirmation service"
            self.record_audit_event(
                "memory_explicit_saved",
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                outcome="rejected",
                summary="memory save source rejected",
                counts={"attempted": 1, "written": 0, "rejected": 1},
                metadata={**source_metadata, "reason": reason},
            )
            raise ValueError(reason)
        decision = self.write_policy.evaluate_explicit_save(text=text, content=content, scope=scope)
        if not decision.allowed:
            if decision.require_user_confirmation:
                confirmation = self._create_pending_confirmation(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                    content=content,
                    memory_id=memory_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    scope=scope,
                    decision=decision,
                    created_at=created_at,
                )
                self.record_audit_event(
                    "memory_confirmation_created",
                    user_id=user_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    session_id=session_id,
                    memory_id=memory_id,
                    summary="explicit memory confirmation required",
                    counts={"attempted": 1, "pending": 1, "written": 0, "rejected": 0},
                    metadata={
                        **promotion_decision_audit_record(decision),
                        **source_metadata,
                        "confirmation_id": confirmation.confirmation_id,
                    },
                )
                raise MemoryConfirmationRequired(confirmation)
            self.record_audit_event(
                "memory_explicit_saved",
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                outcome="rejected",
                summary="explicit memory rejected",
                counts={"attempted": 1, "written": 0, "rejected": 1},
                metadata={**promotion_decision_audit_record(decision), **source_metadata},
            )
            raise ValueError(decision.reason)
        if resolved_source_intent == "assistant_candidate":
            candidate = _memory_save_candidate_result(
                user_id=user_id,
                session_id=session_id,
                decision=decision,
                source_reason=source_reason,
                future_use=future_use,
                evidence=evidence,
            )
            self.record_audit_event(
                "memory_promotion_decided",
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                outcome="skipped",
                summary="assistant memory candidate recorded",
                counts={"candidates": 1, "written": 0, "rejected": 0},
                metadata={
                    **promotion_decision_audit_record(decision),
                    **source_metadata,
                    "candidate_id": candidate.candidate_id,
                    "written": False,
                },
            )
            return candidate
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
        try:
            saved = self._merge_or_save(item)
        except _MemoryFactConflictRequired as exc:
            confirmation = self._create_conflict_confirmation(
                item=exc.item,
                content=content or {},
                write_decision=decision,
                conflict_decision=exc.decision,
            )
            self.record_audit_event(
                "memory_confirmation_created",
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                memory_id=exc.item.memory_id,
                summary="structured memory conflict confirmation required",
                counts={"attempted": 1, "pending": 1, "written": 0, "conflicts": 1},
                metadata={
                    "confirmation_id": confirmation.confirmation_id,
                    "confirmation_kind": confirmation.confirmation_kind,
                    **_conflict_audit_metadata(exc.decision),
                    **source_metadata,
                },
            )
            raise MemoryConfirmationRequired(confirmation) from None
        except MemoryServiceOperationError as exc:
            self._record_remote_lifecycle_failure(
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                operation=exc.operation,
                recoverable=exc.recoverable,
            )
            raise
        except Exception:
            self._record_remote_lifecycle_failure(
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                operation="save_explicit",
                recoverable=True,
            )
            raise
        self._upsert_user_profile(saved)
        framework_queued = saved.content.get("_framework_retain_status") == "queued"
        self.record_audit_event(
            "memory_explicit_saved",
            user_id=saved.user_id,
            tenant_id=saved.tenant_id,
            project_id=saved.project_id,
            session_id=saved.session_id,
            memory_id=saved.memory_id,
            summary=("explicit memory queued for framework retry" if framework_queued else "explicit memory saved"),
            counts={
                "attempted": 1,
                "written": 0 if framework_queued else 1,
                "pending_retry": 1 if framework_queued else 0,
                "rejected": 0,
                "superseded": len(_supersedes_memory_ids(saved)),
            },
            metadata={
                "memory_type": saved.memory_type,
                "scope": memory_scope_for_item(saved),
                "sensitivity": saved.sensitivity,
                "destination": decision.destination,
                "ttl_days": decision.ttl_days,
                "preference_key": _preference_conflict_key(saved),
                "supersedes_memory_ids": _supersedes_memory_ids(saved),
                "framework_retain_status": "queued" if framework_queued else "completed",
                **source_metadata,
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
        source_intent: MemorySaveSourceIntent | None = None,
        source_reason: str | None = None,
        future_use: str | None = None,
        evidence: str | None = None,
        created_at: datetime | None = None,
    ) -> MemoryItem | MemorySaveCandidateResult:
        """Persist or record memory for an identity, ignoring caller user_id."""

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
            source_intent=source_intent,
            source_reason=source_reason,
            future_use=future_use,
            evidence=evidence,
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
        if item is None and bool(getattr(self.store, "framework_managed_algorithms", False)):
            delete_for_identity = getattr(self.store, "delete_for_identity", None)
            deleted = bool(delete_for_identity(identity, memory_id)) if callable(delete_for_identity) else False
            if deleted:
                self.record_audit_event(
                    "memory_deleted",
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    project_id=identity.project_id,
                    session_id=identity.session_id,
                    memory_id=memory_id,
                    summary="pending framework memory deleted",
                    counts={"memory_items": 0, "pending_retry": 1},
                    metadata={"mode": "framework", "pending_retain_cancelled": True},
                )
            return deleted
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
        confirmations_deleted = self._delete_confirmations_for_identity(
            identity,
            session_id=resolved_session_id,
        )
        self.record_audit_event(
            "memory_session_deleted",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=resolved_session_id,
            summary="session memory deleted",
            counts={"memory_items": deleted, "confirmations": confirmations_deleted},
        )
        return deleted

    def clear_user(self, user_id: str) -> None:
        self.clear_identity(RequestIdentity.for_user(user_id=user_id))

    def clear_identity(self, identity: RequestIdentity) -> None:
        deleted = 0
        for item in self.list_for_identity(identity):
            if self.store.delete(identity.user_id, item.memory_id):
                deleted += 1
        confirmations_deleted = self._delete_confirmations_for_identity(identity)
        self.record_audit_event(
            "memory_user_cleared",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=identity.session_id,
            summary="user memory cleared",
            counts={"memory_items": deleted, "confirmations": confirmations_deleted},
        )

    def list_confirmations_for_identity(
        self,
        identity: RequestIdentity,
        *,
        include_resolved: bool = False,
    ) -> list[MemoryPendingConfirmation]:
        """List memory confirmations visible to an identity."""

        confirmations_by_id: dict[str, MemoryPendingConfirmation] = {}
        list_confirmations = getattr(self.store, "list_confirmations", None)
        if callable(list_confirmations):
            for confirmation in list_confirmations(
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                include_resolved=True,
                limit=1000,
            ):
                confirmations_by_id[confirmation.confirmation_id] = confirmation
        for confirmation in self._pending_confirmations.values():
            confirmations_by_id[confirmation.confirmation_id] = confirmation

        confirmations: list[MemoryPendingConfirmation] = []
        for confirmation in confirmations_by_id.values():
            if not _identity_allows_confirmation(identity, confirmation):
                continue
            current = _confirmation_with_expired_status(confirmation)
            if not include_resolved and current.status != "pending":
                continue
            confirmations.append(current)
        return sorted(confirmations, key=lambda item: item.created_at, reverse=True)

    def get_confirmation_for_identity(
        self,
        identity: RequestIdentity,
        confirmation_id: str,
    ) -> MemoryPendingConfirmation | None:
        """Return one visible memory confirmation if it exists."""

        confirmation = self._get_confirmation(identity.user_id, confirmation_id)
        if confirmation is None or not _identity_allows_confirmation(identity, confirmation):
            return None
        return _confirmation_with_expired_status(confirmation)

    def confirm_memory_for_identity(
        self,
        identity: RequestIdentity,
        confirmation_id: str,
        *,
        created_at: datetime | None = None,
    ) -> MemoryPendingConfirmation | None:
        """Confirm a pending explicit memory and persist the redacted payload."""

        confirmation = self._get_confirmation(identity.user_id, confirmation_id)
        if confirmation is None or not _identity_allows_confirmation(identity, confirmation):
            return None
        if _confirmation_is_expired(confirmation):
            expired = confirmation.model_copy(
                update={"status": "expired", "decided_at": datetime.now(timezone.utc)}
            )
            self._save_confirmation(expired)
            self.record_audit_event(
                "memory_confirmation_decided",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=confirmation.session_id,
                memory_id=confirmation.memory_id,
                outcome="rejected",
                summary="memory confirmation expired",
                counts={"expired": 1, "confirmed": 0, "rejected": 0},
                metadata={"confirmation_id": confirmation_id, "status": "expired"},
            )
            raise ValueError("memory confirmation expired")
        if confirmation.status != "pending":
            raise ValueError(f"memory confirmation is {confirmation.status}")

        scope = _confirmation_scope(confirmation)
        content = {
            **confirmation.content_preview,
            "summary": confirmation.summary,
            "consent": "explicit_confirmation",
            "confirmation_id": confirmation.confirmation_id,
        }
        if confirmation.confirmation_kind == "fact_conflict":
            raw_fact = content.get("fact")
            if not isinstance(raw_fact, dict):
                raise ValueError("memory conflict confirmation is missing structured fact")
            fact = fact_from_item(
                MemoryItem(
                    memory_id=confirmation.memory_id or "pending_memory",
                    user_id=identity.user_id,
                    tenant_id=confirmation.tenant_id,
                    project_id=confirmation.project_id,
                    session_id=confirmation.session_id,
                    memory_type=confirmation.memory_type,
                    scope=_confirmation_scope(confirmation),
                    summary=confirmation.summary,
                    content={"fact": raw_fact},
                    source="explicit_user_request",
                    created_at=created_at or datetime.now(timezone.utc),
                )
            )
            if fact is None:
                raise ValueError("memory conflict confirmation fact is invalid")
            content["fact"] = fact.model_copy(
                update={
                    "provenance": "user_confirmed",
                    "conflict_policy": "replace",
                    "status": "active",
                    "supersedes_memory_ids": [],
                    "superseded_by_memory_id": None,
                    "conflict_reason": None,
                }
            ).model_dump(mode="json")
        item = build_explicit_memory_item(
            memory_id=confirmation.memory_id or f"explicit_memory_{uuid4().hex}",
            user_id=identity.user_id,
            session_id=confirmation.session_id or identity.session_id or "default",
            text=confirmation.summary,
            content=content,
            scope=scope,
            policy=self.write_policy,
            created_at=created_at,
        )
        if confirmation.tenant_id is not None or confirmation.project_id is not None or scope is not None:
            item = item.model_copy(
                update={
                    "tenant_id": confirmation.tenant_id,
                    "project_id": confirmation.project_id,
                    "scope": scope or item.scope,
                }
            )
        saved = self._merge_or_save(item)
        self._upsert_user_profile(saved)
        now = datetime.now(timezone.utc)
        confirmed = confirmation.model_copy(
            update={
                "status": "confirmed",
                "decided_at": now,
                "confirmed_memory_id": saved.memory_id,
            }
        )
        self._save_confirmation(confirmed)
        self.record_audit_event(
            "memory_explicit_saved",
            user_id=saved.user_id,
            tenant_id=saved.tenant_id,
            project_id=saved.project_id,
            session_id=saved.session_id,
            memory_id=saved.memory_id,
            summary="confirmed explicit memory saved",
            counts={
                "attempted": 1,
                "written": 1,
                "rejected": 0,
                "superseded": len(_supersedes_memory_ids(saved)),
            },
            metadata={
                "memory_type": saved.memory_type,
                "scope": memory_scope_for_item(saved),
                "sensitivity": saved.sensitivity,
                "destination": confirmation.destination,
                "confirmed_from_confirmation": True,
                "confirmation_id": confirmation_id,
                "source_intent": "user_confirmed",
                "source_reason": "user confirmed pending memory write",
                "future_use": "confirmed memory can be used for future recall",
                "evidence": f"confirmation_id={confirmation_id}",
                "preference_key": _preference_conflict_key(saved),
                "supersedes_memory_ids": _supersedes_memory_ids(saved),
            },
        )
        self.record_audit_event(
            "memory_confirmation_decided",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=confirmation.session_id,
            memory_id=saved.memory_id,
            summary="memory confirmation accepted",
            counts={"confirmed": 1, "rejected": 0, "expired": 0},
            metadata={
                "confirmation_id": confirmation_id,
                "status": "confirmed",
                "confirmed_memory_id": saved.memory_id,
            },
        )
        return confirmed

    def reject_memory_for_identity(
        self,
        identity: RequestIdentity,
        confirmation_id: str,
    ) -> MemoryPendingConfirmation | None:
        """Reject a pending explicit-memory confirmation without persisting memory."""

        confirmation = self._get_confirmation(identity.user_id, confirmation_id)
        if confirmation is None or not _identity_allows_confirmation(identity, confirmation):
            return None
        if confirmation.status != "pending":
            raise ValueError(f"memory confirmation is {confirmation.status}")
        status: MemoryConfirmationStatus = "expired" if _confirmation_is_expired(confirmation) else "rejected"
        decided = confirmation.model_copy(
            update={"status": status, "decided_at": datetime.now(timezone.utc)}
        )
        self._save_confirmation(decided)
        self.record_audit_event(
            "memory_confirmation_decided",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=confirmation.session_id,
            memory_id=confirmation.memory_id,
            outcome="rejected",
            summary="memory confirmation rejected" if status == "rejected" else "memory confirmation expired",
            counts={
                "confirmed": 0,
                "rejected": 1 if status == "rejected" else 0,
                "expired": 1 if status == "expired" else 0,
            },
            metadata={"confirmation_id": confirmation_id, "status": status},
        )
        return decided

    def _delete_confirmations_for_identity(
        self,
        identity: RequestIdentity,
        *,
        session_id: str | None = None,
    ) -> int:
        confirmation_ids = [
            confirmation.confirmation_id
            for confirmation in self.list_confirmations_for_identity(identity, include_resolved=True)
            if _identity_allows_confirmation(identity, confirmation)
            and (session_id is None or confirmation.session_id == session_id)
        ]
        deleted = 0
        for confirmation_id in confirmation_ids:
            if self._delete_confirmation(identity.user_id, confirmation_id):
                deleted += 1
        return deleted

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
        all_profile_items = _profile_all_source_items(self.list_for_identity(identity))
        source_items = _profile_source_items(all_profile_items)
        superseded_source_ids = _profile_superseded_source_ids(all_profile_items)
        profile_conflicts = _profile_conflict_groups(all_profile_items)
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
            profile_conflicts=profile_conflicts,
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
            superseded_source_memory_ids=superseded_source_ids,
            profile_conflicts=profile_conflicts,
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
                    "superseded_sources": len(result.superseded_source_memory_ids),
                    "conflicts": len(result.profile_conflicts),
                    "issues": len(result.issues),
                    "repaired": 1 if result.repaired else 0,
                },
                metadata={
                    "action": result.action,
                    "issues": result.issues,
                    "missing_source_memory_ids": result.missing_source_memory_ids[:50],
                    "stale_source_memory_ids": result.stale_source_memory_ids[:50],
                    "superseded_source_memory_ids": result.superseded_source_memory_ids[:50],
                    "profile_conflicts": result.profile_conflicts[:20],
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

    def _record_remote_search_event(
        self,
        *,
        identity: RequestIdentity,
        result: MemorySearchResult,
    ) -> None:
        error_codes = _remote_error_codes(result.errors)
        if not error_codes:
            return
        active_store = type(self.store).__name__
        if active_store == "FrameworkMemoryStore":
            self.record_audit_event(
                "memory_framework_degraded",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=identity.session_id,
                outcome="failed",
                summary="memory framework recall degraded",
                counts={"attempted": 1, "failed": 1, "fallback_results": result.total},
                metadata={
                    "operation": "recall",
                    "mode": "framework",
                    "active_store": active_store,
                    "error_code": error_codes[0],
                    "recoverable": True,
                },
            )
            return
        if active_store == "RemoteServiceMemoryStore":
            self.record_audit_event(
                "memory_remote_lifecycle_failed",
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                project_id=identity.project_id,
                session_id=identity.session_id,
                outcome="failed",
                summary="remote memory lifecycle search failed",
                counts={"attempted": 1, "failed": 1},
                metadata={
                    "operation": "search",
                    "mode": "remote_service",
                    "active_store": active_store,
                    "error_code": error_codes[0],
                    "recoverable": True,
                },
            )
            return
        self.record_audit_event(
            "memory_remote_degraded",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=identity.session_id,
            outcome="failed",
            summary="remote memory query degraded to local results",
            counts={
                "attempted": 1,
                "failed": 1,
                "local_results": result.total,
            },
            metadata={
                "operation": "search",
                "mode": "dual_core",
                "active_store": active_store,
                "error_codes": error_codes,
            },
        )

    def _record_remote_lifecycle_failure(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        project_id: str | None,
        session_id: str | None,
        operation: str,
        recoverable: bool,
    ) -> None:
        if type(self.store).__name__ != "RemoteServiceMemoryStore":
            return
        self.record_audit_event(
            "memory_remote_lifecycle_failed",
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            outcome="failed",
            summary="remote memory lifecycle operation failed",
            counts={"attempted": 1, "failed": 1},
            metadata={
                "operation": operation,
                "mode": "remote_service",
                "active_store": type(self.store).__name__,
                "error_code": f"memory_remote_service_{operation}_failed",
                "recoverable": recoverable,
            },
        )

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

    def _create_pending_confirmation(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        content: dict[str, Any] | None,
        memory_id: str | None,
        tenant_id: str | None,
        project_id: str | None,
        scope: MemoryScope | None,
        decision: Any,
        created_at: datetime | None = None,
    ) -> MemoryPendingConfirmation:
        now = created_at or datetime.now(timezone.utc)
        redacted_payload = sanitize_error_detail(decision.redacted_payload)
        if not isinstance(redacted_payload, dict):
            redacted_payload = {}
        summary = str(redacted_payload.get("summary") or sanitize_error_message(text)).strip()
        confirmation = MemoryPendingConfirmation(
            confirmation_id=f"memory_confirmation_{uuid4().hex}",
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            memory_id=memory_id or f"explicit_memory_{uuid4().hex}",
            status="pending",
            memory_type=_memory_type_from_confirmation_payload(redacted_payload),
            scope=scope,
            destination=str(decision.destination),
            sensitivity=str(decision.sensitivity),
            reason=sanitize_error_message(decision.reason),
            summary=summary or "pending memory confirmation",
            redacted_payload=redacted_payload,
            content_preview=_safe_explicit_content_preview(content or {}, summary=summary),
            created_at=now,
            expires_at=now + timedelta(seconds=self.default_confirmation_ttl_seconds),
        )
        return self._save_confirmation(confirmation)

    def _create_conflict_confirmation(
        self,
        *,
        item: MemoryItem,
        content: dict[str, Any],
        write_decision: Any,
        conflict_decision: MemoryConflictDecision,
    ) -> MemoryPendingConfirmation:
        now = item.created_at
        confirmation = MemoryPendingConfirmation(
            confirmation_id=f"memory_confirmation_{uuid4().hex}",
            user_id=item.user_id,
            tenant_id=item.tenant_id,
            project_id=item.project_id,
            session_id=item.session_id,
            memory_id=item.memory_id,
            confirmation_kind="fact_conflict",
            fact_key=conflict_decision.fact_key,
            conflict_memory_ids=conflict_decision.matching_memory_ids,
            status="pending",
            memory_type=item.memory_type,
            scope=memory_scope_for_item(item),
            destination=str(write_decision.destination),
            sensitivity=str(write_decision.sensitivity),
            reason=sanitize_error_message(conflict_decision.reason),
            summary=item.summary,
            redacted_payload={
                "summary": item.summary,
                "memory_type": item.memory_type,
                "destination": str(write_decision.destination),
            },
            content_preview=_safe_explicit_content_preview(content, summary=item.summary),
            created_at=now,
            expires_at=now + timedelta(seconds=self.default_confirmation_ttl_seconds),
        )
        return self._save_confirmation(confirmation)

    def _save_confirmation(self, confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
        self._pending_confirmations[confirmation.confirmation_id] = confirmation
        save_confirmation = getattr(self.store, "save_confirmation", None)
        if callable(save_confirmation):
            return save_confirmation(confirmation)
        return confirmation

    def _get_confirmation(self, user_id: str, confirmation_id: str) -> MemoryPendingConfirmation | None:
        confirmation = self._pending_confirmations.get(confirmation_id)
        if confirmation is not None and confirmation.user_id == user_id:
            return confirmation
        get_confirmation = getattr(self.store, "get_confirmation", None)
        if callable(get_confirmation):
            return get_confirmation(user_id, confirmation_id)
        return None

    def _delete_confirmation(self, user_id: str, confirmation_id: str) -> bool:
        deleted = False
        confirmation = self._pending_confirmations.get(confirmation_id)
        if confirmation is not None and confirmation.user_id == user_id:
            del self._pending_confirmations[confirmation_id]
            deleted = True
        delete_confirmation = getattr(self.store, "delete_confirmation", None)
        if callable(delete_confirmation):
            deleted = bool(delete_confirmation(user_id, confirmation_id)) or deleted
        return deleted

    def _merge_or_save(self, item: MemoryItem) -> MemoryItem:
        if bool(getattr(self.store, "framework_managed_algorithms", False)):
            return self.store.save(item)
        conflict = self.conflict_resolver.resolve(item, self.store.list_by_user(item.user_id))
        if conflict.action == "merge":
            existing = self.store.get(item.user_id, conflict.matching_memory_ids[0])
            if existing is not None:
                return self._merge_items(existing, item)
        if conflict.action == "supersede":
            return self._save_with_supersedes(item, conflict)
        if conflict.action == "confirm":
            raise _MemoryFactConflictRequired(item, conflict)
        if fact_from_item(item) is not None:
            return self.store.save(item)

        duplicate = self._find_duplicate(item)
        if duplicate is None:
            return self.store.save(item)

        return self._merge_items(duplicate, item)

    def _merge_items(self, duplicate: MemoryItem, item: MemoryItem) -> MemoryItem:
        """Merge one repeated observation into the canonical item."""

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

    def _save_with_supersedes(
        self,
        item: MemoryItem,
        decision: MemoryConflictDecision,
    ) -> MemoryItem:
        for memory_id in decision.superseded_memory_ids:
            existing = self.store.get(item.user_id, memory_id)
            if existing is None:
                continue
            self.store.save(
                mark_fact_superseded(
                    existing,
                    by_memory_id=item.memory_id,
                    at=item.created_at,
                    reason=decision.reason,
                )
            )

        fact = fact_from_item(item)
        if fact is None:
            return self.store.save(item)
        superseded_ids = sorted(set(decision.superseded_memory_ids))
        updated_fact = fact.model_copy(
            update={
                "status": "active",
                "supersedes_memory_ids": superseded_ids,
                "superseded_by_memory_id": None,
                "conflict_reason": decision.reason,
            }
        )
        content = {
            **item.content,
            **fact_content(updated_fact),
            "supersedes_memory_id": superseded_ids[0] if superseded_ids else None,
            "supersedes_memory_ids": superseded_ids,
            "conflict_reason": decision.reason,
        }
        if updated_fact.predicate.startswith("preference."):
            preference_key = updated_fact.predicate.removeprefix("preference.")
            content["preference_key"] = preference_key
            content["conflict_key"] = preference_key
        return self.store.save(item.model_copy(update={"content": content}))

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
        if bool(getattr(self.store, "framework_managed_algorithms", False)):
            return None
        if item.source == "user_profile" or item.memory_type not in {"preference", "product", "task"}:
            return None
        if item.tenant_id is not None or item.project_id is not None or memory_scope_for_item(item) == "project":
            return None

        existing = self.store.get(item.user_id, USER_PROFILE_MEMORY_ID)
        source_items = _profile_source_items(self.store.list_by_user(item.user_id))
        if not source_items:
            return existing

        profile = UserProfileMemory.empty(item.user_id, now=item.updated_at or item.created_at)
        for source_item in sorted(source_items, key=lambda source: source.created_at):
            profile.merge_memory(source_item, now=source_item.updated_at or source_item.created_at)

        profile_item = profile.to_memory_item(session_id=existing.session_id if existing else item.session_id)
        if existing is not None:
            profile_item = profile_item.model_copy(update={"created_at": existing.created_at})
            if (
                existing.summary == profile_item.summary
                and existing.content.get("preferences") == profile_item.content.get("preferences")
                and existing.content.get("facts") == profile_item.content.get("facts")
                and existing.content.get("source_memory_ids") == profile_item.content.get("source_memory_ids")
            ):
                return existing
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


def _identity_allows_confirmation(identity: RequestIdentity, confirmation: MemoryPendingConfirmation) -> bool:
    if confirmation.user_id != identity.user_id:
        return False
    if confirmation.tenant_id is not None and confirmation.tenant_id != identity.tenant_id:
        return False
    if confirmation.project_id is not None and confirmation.project_id != identity.project_id:
        return False
    if confirmation.scope is not None and confirmation.scope not in _allowed_memory_scopes(identity):
        return False
    return True


def _remote_error_codes(errors: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for error in errors:
        code = str(error.get("code") or "").strip()
        if not code.startswith(("memory_server_", "memory_remote_service_", "memory_framework_")):
            continue
        if code not in codes:
            codes.append(code)
    return codes


def _confirmation_is_expired(confirmation: MemoryPendingConfirmation) -> bool:
    if confirmation.status != "pending" or confirmation.expires_at is None:
        return False
    now = datetime.now(tz=confirmation.expires_at.tzinfo or timezone.utc)
    return confirmation.expires_at < now


def _confirmation_with_expired_status(confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
    if not _confirmation_is_expired(confirmation):
        return confirmation
    return confirmation.model_copy(update={"status": "expired"})


def _confirmation_scope(confirmation: MemoryPendingConfirmation) -> MemoryScope | None:
    return cast(MemoryScope, confirmation.scope) if confirmation.scope in _VALID_MEMORY_SCOPES else None


def _memory_type_from_confirmation_payload(payload: dict[str, Any]) -> MemoryType:
    value = str(payload.get("memory_type") or "task")
    return cast(MemoryType, value if value in _VALID_MEMORY_TYPES else "task")


def _safe_explicit_content_preview(payload: dict[str, Any], *, summary: str) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    if summary.strip():
        preview["summary"] = sanitize_error_message(summary)
    for key in _SAFE_EXPLICIT_CONTENT_KEYS:
        if key == "summary":
            continue
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        preview[key] = sanitize_error_detail(value)
    return preview


def _profile_all_source_items(items: list[MemoryItem]) -> list[MemoryItem]:
    return [
        item
        for item in items
        if _is_profile_source_candidate(item)
    ]


def _profile_source_items(items: list[MemoryItem]) -> list[MemoryItem]:
    return [item for item in _profile_all_source_items(items) if is_active_memory_fact(item)]


def _profile_superseded_source_ids(items: list[MemoryItem]) -> list[str]:
    return sorted(
        item.memory_id
        for item in _profile_all_source_items(items)
        if memory_fact_status(item) == "superseded"
    )


def _profile_conflict_groups(items: list[MemoryItem]) -> list[dict[str, Any]]:
    grouped: dict[str, list[MemoryItem]] = {}
    for item in _profile_all_source_items(items):
        fact = fact_from_item(item)
        if fact is not None:
            grouped.setdefault(fact.fact_key, []).append(item)

    conflicts: list[dict[str, Any]] = []
    for fact_key, group_items in sorted(grouped.items()):
        active_items = [item for item in group_items if memory_fact_status(item) == "active"]
        superseded_items = [item for item in group_items if memory_fact_status(item) == "superseded"]
        disputed_items = [item for item in group_items if memory_fact_status(item) == "disputed"]
        active_values = {
            _normalize_for_dedupe(fact.value)
            for item in active_items
            if (fact := fact_from_item(item)) is not None
        }
        active_facts = [fact for item in active_items if (fact := fact_from_item(item)) is not None]
        coexist_values = bool(active_facts) and all(
            fact.conflict_policy == "coexist" for fact in active_facts
        )
        unresolved = (len(active_values) > 1 and not coexist_values) or bool(disputed_items)
        if not superseded_items and not disputed_items and not unresolved:
            continue
        latest_active = max(
            active_items,
            key=lambda item: (item.created_at, item.memory_id),
            default=None,
        )
        sample_fact = fact_from_item(group_items[0])
        payload: dict[str, Any] = {
            "fact_key": fact_key,
            "active_memory_id": latest_active.memory_id if latest_active else None,
            "active_memory_ids": sorted(item.memory_id for item in active_items),
            "superseded_memory_ids": sorted(item.memory_id for item in superseded_items),
            "disputed_memory_ids": sorted(item.memory_id for item in disputed_items),
            "unresolved": unresolved,
        }
        if sample_fact is not None and sample_fact.predicate.startswith("preference."):
            payload["preference_key"] = sample_fact.predicate.removeprefix("preference.")
        conflicts.append(payload)
    return conflicts


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
    profile_conflicts: list[dict[str, Any]],
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
    if any(conflict.get("unresolved") is True for conflict in profile_conflicts):
        issues.append("profile_unresolved_conflicts")
    return list(dict.fromkeys(issues))


def _is_profile_source_candidate(item: MemoryItem) -> bool:
    return (
        item.source != "user_profile"
        and item.memory_type in {"preference", "product", "task"}
        and item.tenant_id is None
        and item.project_id is None
        and memory_scope_for_item(item) != "project"
        and not _is_expired(item)
    )


def _is_superseded(item: MemoryItem) -> bool:
    return memory_fact_status(item) == "superseded"


def _preference_conflict_key(item: MemoryItem) -> str | None:
    if item.memory_type != "preference" or item.source == "user_profile":
        return None
    raw_key = item.content.get("preference_key") or item.content.get("conflict_key")
    if raw_key not in (None, "", [], {}):
        return _normalize_conflict_key(str(raw_key))
    for key in ("style", "budget"):
        if item.content.get(key) not in (None, "", [], {}):
            return key
    if "预算" in item.summary:
        return "budget"
    return None


def _normalize_conflict_key(value: str) -> str:
    normalized = "".join(ch for ch in value.strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return normalized or "preference"


def _supersedes_memory_ids(item: MemoryItem) -> list[str]:
    values = item.content.get("supersedes_memory_ids")
    if isinstance(values, list):
        return [str(value) for value in values if str(value).strip()]
    value = item.content.get("supersedes_memory_id")
    return [str(value)] if value not in (None, "", [], {}) else []


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


def _memory_save_candidate_result(
    *,
    user_id: str,
    session_id: str,
    decision: Any,
    source_reason: str | None,
    future_use: str | None,
    evidence: str | None,
) -> MemorySaveCandidateResult:
    redacted_payload = decision.redacted_payload if isinstance(decision.redacted_payload, dict) else {}
    summary = str(redacted_payload.get("summary") or "").strip()
    memory_type = str(redacted_payload.get("memory_type") or "")
    return MemorySaveCandidateResult(
        candidate_id=f"memory_candidate_{uuid4().hex}",
        user_id=user_id,
        session_id=session_id,
        summary=summary,
        memory_type=memory_type,
        source_reason=_safe_metadata_text(source_reason),
        future_use=_safe_metadata_text(future_use),
        evidence=_safe_metadata_text(evidence),
        reason=sanitize_error_message(str(decision.reason or "")),
    )


def _memory_save_source_metadata(
    *,
    source_intent: MemorySaveSourceIntent,
    source_reason: str | None,
    future_use: str | None,
    evidence: str | None,
) -> dict[str, Any]:
    return {
        "source_intent": source_intent,
        "source_reason": _safe_metadata_text(source_reason),
        "future_use": _safe_metadata_text(future_use),
        "evidence": _safe_metadata_text(evidence),
    }


def _safe_metadata_text(value: str | None) -> str:
    return sanitize_error_message(str(value or "").strip())[:240]


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


def _skipped_memory_context(decision: MemoryReadDecision) -> MemoryContext:
    recall_report = _memory_recall_report(
        identity=None,
        query_text="",
        result=None,
        context=None,
        read_decision=decision,
        profile_item=None,
        visible_items=[],
    )
    return MemoryContext(
        items=[],
        text="",
        summaries=[],
        artifact_refs=[],
        blocks=[],
        total_tokens=0,
        budget_tokens=decision.max_context_tokens or 0,
        omitted_count=0,
        rejected_reasons=[],
        retrieval_version="memory_read_policy_v1",
        read_policy_allowed=False,
        read_policy_reason=decision.reason,
        read_policy=decision.prompt_safe_metadata(),
        trust_policy=decision.trust_policy,
        usage_hint=decision.usage_hint,
        recall_report=recall_report,
    )


def _memory_recall_report(
    *,
    identity: RequestIdentity | None,
    query_text: str,
    result: MemorySearchResult | None,
    context: MemoryContext | None,
    read_decision: MemoryReadDecision | None,
    profile_item: MemoryItem | None,
    visible_items: list[MemoryItem],
) -> dict[str, Any]:
    normalized_query = " ".join(str(query_text or "").split())
    profile_source_ids = []
    if profile_item is not None:
        raw_source_ids = profile_item.content.get("source_memory_ids")
        if isinstance(raw_source_ids, list):
            profile_source_ids = [str(item) for item in raw_source_ids if str(item).strip()]
    return {
        "read_allowed": bool(read_decision.allowed) if read_decision is not None else True,
        "policy_reason": read_decision.reason if read_decision is not None else "",
        "query_present": bool(normalized_query),
        "query_kind": _memory_recall_query_kind(normalized_query, read_decision),
        "query_hash": _memory_query_hash(normalized_query),
        "candidate_count": int(result.total) if result is not None else 0,
        "injected_count": len(context.items) if context is not None else 0,
        "omitted_count": context.omitted_count if context is not None else 0,
        "rejected_reasons": list(context.rejected_reasons) if context is not None else [],
        "search_error_codes": _search_error_codes(result.errors if result is not None else []),
        "retrieval_version": context.retrieval_version if context is not None else "memory_read_policy_v1",
        "profile_source_ids": profile_source_ids,
        "superseded_excluded_count": _superseded_excluded_count(identity, visible_items),
    }


def _search_error_codes(errors: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for error in errors:
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
    return codes


def _memory_query_hash(normalized_query: str) -> str | None:
    if not normalized_query:
        return None
    return hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()


def _memory_recall_query_kind(
    normalized_query: str,
    read_decision: MemoryReadDecision | None,
) -> str:
    if not normalized_query:
        return "empty"
    trigger = (read_decision.trigger if read_decision is not None else None) or ""
    text = normalized_query.lower()
    if any(marker in normalized_query for marker in ("保存的", "已保存", "偏好", "个人偏好", "按我保存", "按我的偏好")):
        return "saved_preference"
    if any(marker in normalized_query for marker in ("继续", "接着", "上次", "刚才", "之前", "上一轮")):
        return "continuation"
    if any(marker in normalized_query for marker in ("历史", "记忆", "记得", "以前", "过去")) or any(
        marker in text for marker in ("previous", "previously", "saved memory", "last time", "last chat")
    ):
        return "history_reference"
    if trigger:
        return "history_reference"
    return "keyword"


def _superseded_excluded_count(identity: RequestIdentity | None, visible_items: list[MemoryItem]) -> int:
    if identity is None:
        return 0
    return sum(
        1
        for item in visible_items
        if _identity_allows_item(identity, item) and _is_superseded(item)
    )


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


def _conflict_audit_metadata(decision: MemoryConflictDecision) -> dict[str, Any]:
    return {
        "conflict_action": decision.action,
        "conflict_reason": decision.reason,
        "fact_key": decision.fact_key,
        "matching_memory_ids": decision.matching_memory_ids[:50],
        "superseded_memory_ids": decision.superseded_memory_ids[:50],
    }


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
