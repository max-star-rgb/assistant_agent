"""Memory audit service."""

from collections import Counter, defaultdict
from datetime import datetime, timezone

from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem, MemoryType
from multimodal_agent.schemas.memory_audit import (
    MemoryAuditEvent,
    MemoryAuditEventList,
    MemoryAuditItem,
    MemoryAuditList,
    MemoryAuditReport,
    MemoryConfirmationList,
    MemoryConfirmationResult,
    MemoryDeleteResult,
    MemoryDuplicateGroup,
    MemoryExport,
    MemoryMetricsReport,
    MemoryProfileRepairResult,
    MemoryRetentionSweepResult,
)


class MemoryAuditService:
    """User-scoped memory inspection and deletion helpers."""

    def __init__(self, memory_manager: MemoryManager) -> None:
        self.memory_manager = memory_manager

    def list_items(
        self,
        *,
        user_id: str,
        memory_type: MemoryType | None = None,
        include_content: bool = False,
    ) -> MemoryAuditList:
        return self.list_items_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            memory_type=memory_type,
            include_content=include_content,
        )

    def list_items_for_identity(
        self,
        identity: RequestIdentity,
        *,
        memory_type: MemoryType | None = None,
        include_content: bool = False,
    ) -> MemoryAuditList:
        items = self._filtered_items(identity=identity, memory_type=memory_type)
        return MemoryAuditList(
            user_id=identity.user_id,
            total=len(items),
            items=[MemoryAuditItem.from_memory(item, include_content=include_content) for item in items],
        )

    def get_item(self, *, user_id: str, memory_id: str, include_content: bool = True) -> MemoryAuditItem | None:
        return self.get_item_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            memory_id=memory_id,
            include_content=include_content,
        )

    def get_item_for_identity(
        self,
        identity: RequestIdentity,
        *,
        memory_id: str,
        include_content: bool = True,
    ) -> MemoryAuditItem | None:
        item = self.memory_manager.get_for_identity(identity, memory_id)
        if item is None:
            return None
        return MemoryAuditItem.from_memory(item, include_content=include_content)

    def delete_item(self, *, user_id: str, memory_id: str) -> MemoryDeleteResult:
        return self.delete_item_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            memory_id=memory_id,
        )

    def delete_item_for_identity(self, identity: RequestIdentity, *, memory_id: str) -> MemoryDeleteResult:
        deleted = 1 if self.memory_manager.delete_for_identity(identity, memory_id) else 0
        return MemoryDeleteResult(user_id=identity.user_id, deleted={"memory_items": deleted})

    def delete_session(self, *, user_id: str, session_id: str) -> MemoryDeleteResult:
        return self.delete_session_for_identity(
            RequestIdentity.for_user(user_id=user_id, session_id=session_id),
        )

    def delete_session_for_identity(
        self,
        identity: RequestIdentity,
        *,
        session_id: str | None = None,
    ) -> MemoryDeleteResult:
        deleted = self.memory_manager.delete_session_for_identity(identity, session_id=session_id)
        return MemoryDeleteResult(user_id=identity.user_id, deleted={"memory_items": deleted})

    def export(self, *, user_id: str, include_content: bool = True) -> MemoryExport:
        return self.export_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            include_content=include_content,
        )

    def export_for_identity(
        self,
        identity: RequestIdentity,
        *,
        include_content: bool = True,
    ) -> MemoryExport:
        items = self.memory_manager.list_for_identity(identity)
        export = MemoryExport(
            user_id=identity.user_id,
            exported_at=datetime.now(timezone.utc),
            include_content=include_content,
            total=len(items),
            items=[MemoryAuditItem.from_memory(item, include_content=include_content) for item in items],
        )
        self.memory_manager.record_audit_event(
            "memory_exported",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=identity.session_id,
            summary="memory export created",
            counts={"memory_items": len(items), "include_content": 1 if include_content else 0},
            metadata={"include_content": include_content},
        )
        return export

    def sweep_expired(
        self,
        *,
        user_id: str,
        hard_delete: bool = False,
        dry_run: bool = False,
    ) -> MemoryRetentionSweepResult:
        return self.sweep_expired_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            hard_delete=hard_delete,
            dry_run=dry_run,
        )

    def sweep_expired_for_identity(
        self,
        identity: RequestIdentity,
        *,
        hard_delete: bool = False,
        dry_run: bool = False,
    ) -> MemoryRetentionSweepResult:
        items = self.memory_manager.list_for_identity(identity)
        expired_items = [item for item in items if _is_expired(item)]
        deleted_count = 0
        if not dry_run:
            for item in expired_items:
                deleted = (
                    self.memory_manager.hard_delete_for_identity(identity, item.memory_id)
                    if hard_delete
                    else self.memory_manager.delete_for_identity(identity, item.memory_id)
                )
                if deleted:
                    deleted_count += 1
        result = MemoryRetentionSweepResult(
            user_id=identity.user_id,
            mode="hard_delete" if hard_delete else "soft_delete",
            dry_run=dry_run,
            scanned=len(items),
            expired=len(expired_items),
            deleted={"memory_items": deleted_count},
            memory_ids=[item.memory_id for item in expired_items],
        )
        self.memory_manager.record_audit_event(
            "memory_retention_swept",
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            session_id=identity.session_id,
            outcome="skipped" if dry_run else "succeeded",
            summary="expired memory retention sweep",
            counts={
                "scanned": result.scanned,
                "expired": result.expired,
                "deleted": deleted_count,
                "hard_delete": 1 if hard_delete else 0,
                "dry_run": 1 if dry_run else 0,
            },
            metadata={"mode": result.mode, "memory_ids": result.memory_ids[:50]},
        )
        return result

    def events(
        self,
        *,
        user_id: str,
        event_type: str | None = None,
        limit: int = 100,
    ) -> MemoryAuditEventList:
        return self.events_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            event_type=event_type,
            limit=limit,
        )

    def events_for_identity(
        self,
        identity: RequestIdentity,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> MemoryAuditEventList:
        items = self.memory_manager.list_audit_events_for_identity(
            identity,
            event_type=event_type,
            limit=limit,
        )
        return MemoryAuditEventList(user_id=identity.user_id, total=len(items), items=items)

    def metrics(self, *, user_id: str) -> MemoryMetricsReport:
        return self.metrics_for_identity(RequestIdentity.for_user(user_id=user_id))

    def metrics_for_identity(self, identity: RequestIdentity) -> MemoryMetricsReport:
        limit = getattr(self.memory_manager, "max_audit_events", 1000)
        events = self.memory_manager.list_audit_events_for_identity(identity, limit=limit)
        by_event_type = Counter(event.event_type for event in events)
        by_outcome = Counter(event.outcome for event in events)
        return MemoryMetricsReport(
            user_id=identity.user_id,
            total_events=len(events),
            by_event_type=dict(by_event_type),
            by_outcome=dict(by_outcome),
            counters=_metrics_counters(events),
        )

    def confirmations(self, *, user_id: str, include_resolved: bool = False) -> MemoryConfirmationList:
        return self.confirmations_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            include_resolved=include_resolved,
        )

    def confirmations_for_identity(
        self,
        identity: RequestIdentity,
        *,
        include_resolved: bool = False,
    ) -> MemoryConfirmationList:
        items = self.memory_manager.list_confirmations_for_identity(
            identity,
            include_resolved=include_resolved,
        )
        return MemoryConfirmationList(user_id=identity.user_id, total=len(items), items=items)

    def confirm_memory(
        self,
        *,
        user_id: str,
        confirmation_id: str,
    ) -> MemoryConfirmationResult | None:
        return self.confirm_memory_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            confirmation_id=confirmation_id,
        )

    def confirm_memory_for_identity(
        self,
        identity: RequestIdentity,
        *,
        confirmation_id: str,
    ) -> MemoryConfirmationResult | None:
        confirmation = self.memory_manager.confirm_memory_for_identity(identity, confirmation_id)
        if confirmation is None:
            return None
        return MemoryConfirmationResult(
            user_id=identity.user_id,
            confirmation_id=confirmation.confirmation_id,
            status=confirmation.status,
            memory_id=confirmation.confirmed_memory_id,
            confirmation=confirmation,
        )

    def reject_memory(
        self,
        *,
        user_id: str,
        confirmation_id: str,
    ) -> MemoryConfirmationResult | None:
        return self.reject_memory_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            confirmation_id=confirmation_id,
        )

    def reject_memory_for_identity(
        self,
        identity: RequestIdentity,
        *,
        confirmation_id: str,
    ) -> MemoryConfirmationResult | None:
        confirmation = self.memory_manager.reject_memory_for_identity(identity, confirmation_id)
        if confirmation is None:
            return None
        return MemoryConfirmationResult(
            user_id=identity.user_id,
            confirmation_id=confirmation.confirmation_id,
            status=confirmation.status,
            memory_id=confirmation.confirmed_memory_id,
            confirmation=confirmation,
        )

    def profile_status(self, *, user_id: str) -> MemoryProfileRepairResult:
        return self.profile_status_for_identity(RequestIdentity.for_user(user_id=user_id))

    def profile_status_for_identity(self, identity: RequestIdentity) -> MemoryProfileRepairResult:
        return self.memory_manager.rebuild_user_profile_for_identity(
            identity,
            dry_run=True,
            record_event=False,
        )

    def rebuild_profile(
        self,
        *,
        user_id: str,
        dry_run: bool = False,
    ) -> MemoryProfileRepairResult:
        return self.rebuild_profile_for_identity(
            RequestIdentity.for_user(user_id=user_id),
            dry_run=dry_run,
        )

    def rebuild_profile_for_identity(
        self,
        identity: RequestIdentity,
        *,
        dry_run: bool = False,
    ) -> MemoryProfileRepairResult:
        return self.memory_manager.rebuild_user_profile_for_identity(
            identity,
            dry_run=dry_run,
            record_event=True,
        )

    def audit(self, *, user_id: str) -> MemoryAuditReport:
        return self.audit_for_identity(RequestIdentity.for_user(user_id=user_id))

    def audit_for_identity(self, identity: RequestIdentity) -> MemoryAuditReport:
        items = self.memory_manager.list_for_identity(identity)
        by_type = Counter(item.memory_type for item in items)
        by_source = Counter(item.source for item in items)
        duplicate_groups = _duplicate_groups(items)
        expired_count = _expired_count(items)
        warnings: list[str] = []
        if duplicate_groups:
            warnings.append("potential_duplicate_memories")
        if expired_count:
            warnings.append("expired_memories_present")
        profile_present = any(item.memory_id == USER_PROFILE_MEMORY_ID and item.source == "user_profile" for item in items)
        if any(item.memory_type == "preference" for item in items) and not profile_present:
            warnings.append("user_profile_missing")
        profile_status = self.profile_status_for_identity(identity)
        for issue in profile_status.issues:
            if issue not in warnings:
                warnings.append(issue)

        return MemoryAuditReport(
            user_id=identity.user_id,
            total=len(items),
            by_type=dict(by_type),
            by_source=dict(by_source),
            sensitive_count=sum(1 for item in items if item.sensitivity == "sensitive"),
            expired_count=expired_count,
            duplicate_groups=duplicate_groups,
            profile_present=profile_present,
            profile_memory_id=USER_PROFILE_MEMORY_ID if profile_present else None,
            warnings=warnings,
        )

    def _filtered_items(self, *, identity: RequestIdentity, memory_type: MemoryType | None) -> list[MemoryItem]:
        items = self.memory_manager.list_for_identity(identity)
        if memory_type is not None:
            items = [item for item in items if item.memory_type == memory_type]
        return items


def _duplicate_groups(items: list[MemoryItem]) -> list[MemoryDuplicateGroup]:
    grouped: dict[tuple[MemoryType, str], list[MemoryItem]] = defaultdict(list)
    for item in items:
        if item.source == "user_profile":
            continue
        key = _dedupe_key(item)
        if not key:
            continue
        grouped[(item.memory_type, key)].append(item)

    duplicates: list[MemoryDuplicateGroup] = []
    for (memory_type, key), group_items in grouped.items():
        if len(group_items) < 2:
            continue
        duplicates.append(
            MemoryDuplicateGroup(
                key=key,
                memory_type=memory_type,
                memory_ids=[item.memory_id for item in group_items],
                summary=group_items[0].summary,
            )
        )
    return sorted(duplicates, key=lambda group: (group.memory_type, group.key))


def _expired_count(items: list[MemoryItem]) -> int:
    return sum(1 for item in items if _is_expired(item))


def _is_expired(item: MemoryItem) -> bool:
    if item.expires_at is None:
        return False
    now = datetime.now(tz=item.expires_at.tzinfo or timezone.utc)
    return item.expires_at < now


def _dedupe_key(item: MemoryItem) -> str:
    return "".join(ch for ch in item.summary.strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _metrics_counters(events: list[MemoryAuditEvent]) -> dict[str, int]:
    counters: Counter[str] = Counter()
    counters["memory.audit.events.count"] = len(events)
    for event in events:
        counters[f"memory.audit.event.{event.event_type}.count"] += 1
        if event.event_type == "memory_context_loaded":
            counters["memory.search.count"] += 1
            if event.counts.get("retrieved", 0) > 0:
                counters["memory.search.hit.count"] += 1
            else:
                counters["memory.search.empty.count"] += 1
            counters["memory.context.injected_tokens"] += event.counts.get("tokens", 0)
            counters["memory.context.omitted_items"] += event.counts.get("omitted", 0)
            counters["memory.context.rejected_items"] += event.counts.get("rejected", 0)
            counters["memory.context.rejected_sensitive_items"] += _sensitive_rejection_count(event)
        elif event.event_type == "memory_explicit_saved":
            counters["memory.write.allowed.count"] += event.counts.get("written", 0)
            counters["memory.write.rejected.count"] += event.counts.get("rejected", 0)
            if event.metadata.get("require_user_confirmation") is True:
                counters["memory.write.needs_confirmation.count"] += 1
        elif event.event_type == "memory_promotion_decided":
            counters["memory.write.allowed.count"] += event.counts.get("written", 0)
            counters["memory.write.rejected.count"] += event.counts.get("rejected", 0)
            if event.metadata.get("require_user_confirmation") is True:
                counters["memory.write.needs_confirmation.count"] += 1
        elif event.event_type == "memory_confirmation_created":
            counters["memory.write.needs_confirmation.count"] += event.counts.get("pending", 0)
            counters["memory.confirmation.pending.count"] += event.counts.get("pending", 0)
        elif event.event_type == "memory_confirmation_decided":
            counters["memory.confirmation.confirmed.count"] += event.counts.get("confirmed", 0)
            counters["memory.confirmation.rejected.count"] += event.counts.get("rejected", 0)
            counters["memory.confirmation.expired.count"] += event.counts.get("expired", 0)
        elif event.event_type == "memory_deleted":
            counters["memory.delete.soft.count"] += event.counts.get("memory_items", 0)
        elif event.event_type == "memory_hard_deleted":
            counters["memory.delete.hard.count"] += event.counts.get("memory_items", 0)
        elif event.event_type in {"memory_session_deleted", "memory_user_cleared"}:
            counters["memory.delete.soft.count"] += event.counts.get("memory_items", 0)
        elif event.event_type == "memory_exported":
            counters["memory.export.count"] += 1
            counters["memory.export.items"] += event.counts.get("memory_items", 0)
        elif event.event_type == "memory_retention_swept":
            counters["memory.ttl.swept.count"] += 1
            counters["memory.ttl.expired.count"] += event.counts.get("expired", 0)
            counters["memory.ttl.deleted.count"] += event.counts.get("deleted", 0)
        elif event.event_type == "memory_profile_repaired":
            counters["memory.profile.update.count"] += event.counts.get("repaired", 0)
            counters["memory.profile.conflict.count"] += event.counts.get(
                "conflicts",
                event.counts.get("issues", 0),
            )
    return dict(sorted(counters.items()))


def _sensitive_rejection_count(event: MemoryAuditEvent) -> int:
    reasons = event.metadata.get("rejected_reasons")
    if not isinstance(reasons, list):
        return 0
    return sum(1 for reason in reasons if "sensitive" in str(reason).lower())
