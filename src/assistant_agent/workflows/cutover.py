"""Operator-approved Workflow engine cutover facts and safe legacy inventory."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from contextlib import AbstractAsyncContextManager
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.workflows.models import (
    WorkflowBundle,
    WorkflowEngineMigration,
    WorkflowEvent,
    WorkflowRetirementAudit,
)
from assistant_agent.workflows.store import WorkflowRevisionConflict, WorkflowStore


CutoverPhase = Literal[
    "cutover_active",
    "rollback_requested",
    "draining",
    "retired",
]
LegacyCutoverClass = Literal[
    "terminal_read_only",
    "migrate_pristine_queued",
    "drain_running",
    "drain_waiting",
]
WorkflowRetirementReasonCode = Literal[
    "nonterminal_legacy_present",
    "active_legacy_leases_present",
    "waiting_legacy_present",
    "manifest_not_retired",
    "rollback_window_open",
    "retirement_audit_missing",
    "retirement_audit_manifest_mismatch",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkflowLegacyCutoverRules(_StrictFrozenModel):
    terminal: Literal["read_only"]
    pristine_queued: Literal["migrate_two_phase"]
    running: Literal["drain_allowlist"]
    waiting: Literal["drain_allowlist"]


class WorkflowEngineCutoverManifest(_StrictFrozenModel):
    """Versioned local operator decision; secrets and inferred policy are rejected."""

    schema_version: Literal["workflow_engine_cutover_v1"]
    revision: int = Field(ge=1)
    phase: CutoverPhase
    new_submission_engine: Literal["langgraph_v3"]
    legacy_rules: WorkflowLegacyCutoverRules
    drain_deadline: datetime
    rollback_deadline: datetime
    operator_approval_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")

    @model_validator(mode="after")
    def validate_deadlines(self) -> "WorkflowEngineCutoverManifest":
        if self.drain_deadline.tzinfo is None or self.rollback_deadline.tzinfo is None:
            raise ValueError("cutover deadlines must be timezone-aware")
        if self.rollback_deadline < self.drain_deadline:
            raise ValueError("rollback deadline must not precede drain deadline")
        return self

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


class WorkflowCutoverInventory(_StrictFrozenModel):
    """Content-free aggregate used by the operator and retirement gate."""

    counts: dict[LegacyCutoverClass, int]
    nonterminal_legacy_count: int = Field(ge=0)


class WorkflowRetirementStatus(_StrictFrozenModel):
    """Content-free machine proof for removal of the legacy execution path."""

    schema_version: Literal["workflow_retirement_status_v1"] = (
        "workflow_retirement_status_v1"
    )
    ready: bool
    nonterminal_legacy_count: int = Field(ge=0)
    active_legacy_lease_count: int = Field(ge=0)
    waiting_legacy_count: int = Field(ge=0)
    manifest_phase: CutoverPhase
    manifest_revision: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rollback_closed: bool
    retirement_audit_present: bool
    reason_codes: tuple[WorkflowRetirementReasonCode, ...] = Field(max_length=7)


class WorkflowCutoverController:
    """Classify legacy rows using only persisted structured execution facts."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        manifest: WorkflowEngineCutoverManifest,
        manifest_source: Callable[[], WorkflowEngineCutoverManifest] | None = None,
    ) -> None:
        self.store = store
        self.manifest = manifest
        self._manifest_source = manifest_source

    def refresh_manifest(self) -> WorkflowEngineCutoverManifest:
        if self._manifest_source is None:
            return self.manifest
        current = self._manifest_source()
        if current.revision < self.manifest.revision:
            raise ValueError("workflow cutover manifest revision moved backwards")
        if (
            current.revision == self.manifest.revision
            and current.digest != self.manifest.digest
        ):
            raise ValueError("same manifest revision must be immutable")
        self.manifest = current
        return current

    def inventory(self) -> WorkflowCutoverInventory:
        list_bundles = getattr(self.store, "list_cutover_bundles", None)
        if not callable(list_bundles):
            raise TypeError("workflow store does not support cutover inventory")
        counts: Counter[LegacyCutoverClass] = Counter()
        for bundle in list_bundles():
            if not isinstance(bundle, WorkflowBundle):
                raise TypeError("workflow cutover inventory returned an invalid bundle")
            if bundle.workflow.execution_engine != "legacy_scheduler_v2":
                continue
            counts[self._classify(bundle)] += 1
        ordered: dict[LegacyCutoverClass, int] = {
            name: counts[name]
            for name in (
                "terminal_read_only",
                "migrate_pristine_queued",
                "drain_running",
                "drain_waiting",
            )
        }
        return WorkflowCutoverInventory(
            counts=ordered,
            nonterminal_legacy_count=sum(ordered.values())
            - ordered["terminal_read_only"],
        )

    def retirement_status(self, *, now: datetime) -> WorkflowRetirementStatus:
        """Recompute every legacy-removal prerequisite from persisted facts."""

        if now.tzinfo is None:
            raise ValueError("retirement status timestamp must be timezone-aware")
        self.refresh_manifest()
        list_bundles = getattr(self.store, "list_cutover_bundles", None)
        if not callable(list_bundles):
            raise TypeError("workflow store does not support cutover inventory")
        bundles = list_bundles()
        if any(not isinstance(bundle, WorkflowBundle) for bundle in bundles):
            raise TypeError("workflow cutover inventory returned an invalid bundle")
        legacy = [
            bundle
            for bundle in bundles
            if bundle.workflow.execution_engine == "legacy_scheduler_v2"
        ]
        nonterminal_count = sum(
            bundle.workflow.status not in {"completed", "failed", "cancelled"}
            for bundle in legacy
        )
        waiting_count = sum(
            bundle.workflow.status in {"waiting_input", "blocked"} for bundle in legacy
        )
        active_lease_count = sum(
            item.lease_expires_at is not None and item.lease_expires_at > now
            for bundle in legacy
            for plan in bundle.plans
            for item in plan.work_items
        )
        rollback_closed = now > self.manifest.rollback_deadline
        list_audits = getattr(self.store, "list_retirement_audits", None)
        if not callable(list_audits):
            raise TypeError("workflow store does not support retirement audit facts")
        audits = list_audits()
        audit_present = any(
            audit.manifest_revision == self.manifest.revision
            and audit.manifest_digest == self.manifest.digest
            and audit.operator_approval_ref == self.manifest.operator_approval_ref
            for audit in audits
        )
        reasons: list[WorkflowRetirementReasonCode] = []
        if nonterminal_count:
            reasons.append("nonterminal_legacy_present")
        if active_lease_count:
            reasons.append("active_legacy_leases_present")
        if waiting_count:
            reasons.append("waiting_legacy_present")
        if self.manifest.phase != "retired":
            reasons.append("manifest_not_retired")
        if not rollback_closed:
            reasons.append("rollback_window_open")
        if not audit_present:
            reasons.append(
                "retirement_audit_manifest_mismatch"
                if audits
                else "retirement_audit_missing"
            )
        return WorkflowRetirementStatus(
            ready=not reasons,
            nonterminal_legacy_count=nonterminal_count,
            active_legacy_lease_count=active_lease_count,
            waiting_legacy_count=waiting_count,
            manifest_phase=self.manifest.phase,
            manifest_revision=self.manifest.revision,
            manifest_digest=self.manifest.digest,
            rollback_closed=rollback_closed,
            retirement_audit_present=audit_present,
            reason_codes=tuple(reasons),
        )

    def record_retirement_audit(self, *, now: datetime) -> WorkflowRetirementAudit:
        """Persist approval only after every non-audit retirement fact is true."""

        status = self.retirement_status(now=now)
        if not status.reason_codes:
            return next(
                audit
                for audit in self.store.list_retirement_audits()
                if audit.manifest_revision == self.manifest.revision
                and audit.manifest_digest == self.manifest.digest
                and audit.operator_approval_ref == self.manifest.operator_approval_ref
            )
        if status.reason_codes == ("retirement_audit_manifest_mismatch",):
            raise ValueError("retirement_audit_manifest_conflict")
        if status.reason_codes != ("retirement_audit_missing",):
            raise ValueError("workflow_retirement_prerequisites_not_satisfied")
        audit = WorkflowRetirementAudit(
            manifest_revision=self.manifest.revision,
            manifest_digest=self.manifest.digest,
            operator_approval_ref=self.manifest.operator_approval_ref,
            created_at=now,
        )
        record = getattr(self.store, "record_retirement_audit", None)
        if not callable(record):
            raise TypeError("workflow store does not support retirement audit facts")
        persisted = record(audit)
        if not isinstance(persisted, WorkflowRetirementAudit):
            raise TypeError("workflow store returned an invalid retirement audit")
        return persisted

    def legacy_drain_allowlist(self) -> frozenset[str]:
        """Freeze exact existing non-migratable legacy owners for the drain host."""

        list_bundles = getattr(self.store, "list_cutover_bundles", None)
        if not callable(list_bundles):
            raise TypeError("workflow store does not support cutover inventory")
        return frozenset(
            bundle.workflow.workflow_id
            for bundle in list_bundles()
            if bundle.workflow.execution_engine == "legacy_scheduler_v2"
            and self._classify(bundle) in {"drain_running", "drain_waiting"}
        )

    def pristine_migration_ids(self) -> tuple[str, ...]:
        """Return only existing prepared or pristine queued legacy rows."""

        list_bundles = getattr(self.store, "list_cutover_bundles", None)
        if not callable(list_bundles):
            raise TypeError("workflow store does not support cutover inventory")
        candidates: list[str] = []
        for bundle in list_bundles():
            workflow = bundle.workflow
            migration = workflow.engine_migration
            if workflow.execution_engine != "legacy_scheduler_v2":
                continue
            if migration is not None and migration.status == "prepared":
                candidates.append(workflow.workflow_id)
            elif _is_pristine_queued(bundle):
                candidates.append(workflow.workflow_id)
        return tuple(sorted(candidates))

    def prepare_pristine_queued(self, workflow_id: str) -> WorkflowBundle:
        self.refresh_manifest()
        if self.manifest.phase not in {"cutover_active", "draining"}:
            raise ValueError("current cutover phase does not allow migration prepare")
        bundle = self._require_bundle(workflow_id)
        existing = bundle.workflow.engine_migration
        if existing is not None:
            if existing.status == "prepared":
                return bundle
            raise ValueError("workflow migration is already terminal")
        if (
            bundle.workflow.execution_engine != "legacy_scheduler_v2"
            or not _is_pristine_queued(bundle)
        ):
            raise ValueError("only pristine queued legacy workflows can migrate")
        changed = bundle.model_copy(deep=True)
        workflow = changed.workflow
        workflow.engine_migration = WorkflowEngineMigration(
            status="prepared",
            workflow_thread_id=_workflow_thread_id(workflow_id),
            idempotency_key=_migration_idempotency_key(
                workflow_id=workflow_id,
                source_revision=workflow.revision,
                manifest_digest=self.manifest.digest,
            ),
            source_revision=workflow.revision,
            manifest_revision=self.manifest.revision,
            manifest_digest=self.manifest.digest,
        )
        workflow.legacy_claim_frozen = True
        try:
            return self.store.save(
                changed,
                expected_revision=bundle.workflow.revision,
                events=[
                    WorkflowEvent(
                        workflow_id=workflow_id,
                        event_type="workflow.engine.migration_prepared",
                        status="queued",
                        payload={
                            "schema_version": "workflow_engine_migration_v1",
                            "manifest_digest": self.manifest.digest,
                        },
                    )
                ],
            )
        except WorkflowRevisionConflict:
            raced = self._require_bundle(workflow_id)
            migration = raced.workflow.engine_migration
            if migration is not None and migration.status == "prepared":
                return raced
            raise

    async def commit_prepared(
        self,
        workflow_id: str,
        *,
        graph_host: "WorkflowMigrationGraphHost",
    ) -> WorkflowBundle:
        async with graph_host.migration_guard(workflow_id=workflow_id):
            self.refresh_manifest()
            bundle = self._require_bundle(workflow_id)
            migration = bundle.workflow.engine_migration
            if migration is not None and migration.status == "committed":
                return bundle
            if migration is None or migration.status != "prepared":
                raise ValueError("workflow migration is not prepared")
            self._validate_manifest_revision(migration)
            if not await graph_host.has_checkpoint(workflow_id=workflow_id):
                raise ValueError("workflow migration checkpoint proof is required")
            changed = bundle.model_copy(deep=True)
            changed.workflow.execution_engine = "langgraph_v3"
            changed.workflow.engine_migration = migration.model_copy(
                update={"status": "committed"}
            )
            changed.workflow.legacy_claim_frozen = False
            try:
                return self.store.save(
                    changed,
                    expected_revision=bundle.workflow.revision,
                    events=[
                        WorkflowEvent(
                            workflow_id=workflow_id,
                            event_type="workflow.engine.migration_committed",
                            status="queued",
                            payload={
                                "schema_version": "workflow_engine_migration_v1",
                                "manifest_digest": migration.manifest_digest,
                            },
                        )
                    ],
                )
            except WorkflowRevisionConflict:
                raced = self._require_bundle(workflow_id)
                current = raced.workflow.engine_migration
                if current is not None and current.status == "committed":
                    return raced
                raise

    async def rollback_prepared(
        self,
        workflow_id: str,
        *,
        graph_host: "WorkflowMigrationGraphHost",
        now: datetime | None = None,
    ) -> WorkflowBundle:
        async with graph_host.migration_guard(workflow_id=workflow_id):
            self.refresh_manifest()
            observed = now or datetime.now(timezone.utc)
            if (
                self.manifest.phase != "rollback_requested"
                or observed > self.manifest.rollback_deadline
            ):
                raise ValueError("operator rollback phase or window is not active")
            bundle = self._require_bundle(workflow_id)
            migration = bundle.workflow.engine_migration
            if migration is None or migration.status != "prepared":
                raise ValueError("workflow migration is not rollback eligible")
            self._validate_manifest_revision(migration)
            if await graph_host.has_checkpoint(workflow_id=workflow_id):
                raise ValueError(
                    "workflow migration checkpoint exists; rollback is unsafe"
                )
            changed = bundle.model_copy(deep=True)
            changed.workflow.engine_migration = migration.model_copy(
                update={"status": "rolled_back"}
            )
            changed.workflow.legacy_claim_frozen = False
            try:
                return self.store.save(
                    changed,
                    expected_revision=bundle.workflow.revision,
                    events=[
                        WorkflowEvent(
                            workflow_id=workflow_id,
                            event_type="workflow.engine.migration_rolled_back",
                            status="queued",
                            payload={
                                "schema_version": "workflow_engine_migration_v1",
                                "manifest_digest": migration.manifest_digest,
                            },
                        )
                    ],
                )
            except WorkflowRevisionConflict:
                raced = self._require_bundle(workflow_id)
                current = raced.workflow.engine_migration
                if current is not None and current.status == "rolled_back":
                    return raced
                raise

    def _validate_manifest_revision(self, migration: WorkflowEngineMigration) -> None:
        current = self.manifest
        if current.revision < migration.manifest_revision:
            raise ValueError("workflow cutover manifest is older than migration")
        if (
            current.revision == migration.manifest_revision
            and current.digest != migration.manifest_digest
        ):
            raise ValueError("workflow cutover manifest revision digest changed")

    def _require_bundle(self, workflow_id: str) -> WorkflowBundle:
        bundle = self.store.load(workflow_id)
        if bundle is None:
            raise ValueError("workflow was not found")
        return bundle

    @staticmethod
    def _classify(bundle: WorkflowBundle) -> LegacyCutoverClass:
        status = bundle.workflow.status
        if status in {"completed", "failed", "cancelled"}:
            return "terminal_read_only"
        if status == "queued" and _is_pristine_queued(bundle):
            return "migrate_pristine_queued"
        if status in {"queued", "running", "recovering"}:
            return "drain_running"
        if status in {"waiting_input", "blocked"}:
            return "drain_waiting"
        raise ValueError(f"unsupported legacy workflow status: {status}")


def load_workflow_cutover_manifest(
    path: str | Path,
) -> WorkflowEngineCutoverManifest:
    """Load the operator-owned untracked JSON manifest without defaults."""

    manifest_path = Path(path)
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise ValueError("workflow cutover manifest path must be an existing file")
    return WorkflowEngineCutoverManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )


def _is_pristine_queued(bundle: WorkflowBundle) -> bool:
    workflow = bundle.workflow
    if (
        workflow.status != "queued"
        or workflow.cancel_requested
        or workflow.waiting_input is not None
        or workflow.result_artifact_refs
    ):
        return False
    return all(
        item.attempt_count == 0
        and item.active_attempt_id is None
        and item.lease_owner is None
        and item.lease_token is None
        and item.lease_expires_at is None
        and item.reserved_model_calls == 0
        and item.reserved_tool_calls == 0
        and not item.output_artifact_refs
        for plan in bundle.plans
        for item in plan.work_items
    )


class WorkflowMigrationGraphHost(Protocol):
    def migration_guard(
        self, *, workflow_id: str
    ) -> AbstractAsyncContextManager[None]: ...

    async def has_checkpoint(self, *, workflow_id: str) -> bool: ...

    async def ensure_started(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
    ) -> None: ...

    async def activate(self, *, workflow_id: str) -> None: ...


class WorkflowMigrationReconciler:
    """Repair either half of the business-record/checkpoint migration barrier."""

    def __init__(
        self,
        *,
        controller: WorkflowCutoverController,
        graph_host: WorkflowMigrationGraphHost,
    ) -> None:
        self.controller = controller
        self.graph_host = graph_host

    async def reconcile_one(self, workflow_id: str) -> str:
        self.controller.refresh_manifest()
        bundle = self.controller._require_bundle(workflow_id)
        migration = bundle.workflow.engine_migration
        if migration is None:
            raise ValueError("workflow migration is not prepared")
        if migration.status == "committed":
            return "already_committed"
        if migration.status == "rolled_back":
            return "already_rolled_back"
        checkpoint_exists = await self.graph_host.has_checkpoint(
            workflow_id=workflow_id
        )
        self.controller.refresh_manifest()
        if (
            not checkpoint_exists
            and self.controller.manifest.phase == "rollback_requested"
        ):
            await self.controller.rollback_prepared(
                workflow_id,
                graph_host=self.graph_host,
            )
            return "migration_rolled_back"
        if not checkpoint_exists:
            await self.graph_host.ensure_started(
                workflow_id=workflow_id,
                idempotency_key=migration.idempotency_key,
            )
            checkpoint_exists = await self.graph_host.has_checkpoint(
                workflow_id=workflow_id
            )
        if not checkpoint_exists:
            raise RuntimeError("workflow graph checkpoint was not created")
        await self.controller.commit_prepared(
            workflow_id,
            graph_host=self.graph_host,
        )
        await self.graph_host.activate(workflow_id=workflow_id)
        return "migration_committed"


def _workflow_thread_id(workflow_id: str) -> str:
    return f"workflow:{workflow_id}"


def _migration_idempotency_key(
    *, workflow_id: str, source_revision: int, manifest_digest: str
) -> str:
    payload = f"{workflow_id}\0{source_revision}\0{manifest_digest}".encode("utf-8")
    return "workflow-migration:sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "WorkflowCutoverController",
    "WorkflowCutoverInventory",
    "WorkflowEngineCutoverManifest",
    "WorkflowLegacyCutoverRules",
    "WorkflowRetirementStatus",
    "WorkflowMigrationReconciler",
    "load_workflow_cutover_manifest",
]
