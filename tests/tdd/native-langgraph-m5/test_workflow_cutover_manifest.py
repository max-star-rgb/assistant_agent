from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.models import WorkflowSubmission
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.store import InMemoryWorkflowStore


def _manifest_payload(*, phase: str = "cutover_active") -> dict[str, object]:
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    return {
        "schema_version": "workflow_engine_cutover_v1",
        "revision": 7 if phase == "cutover_active" else 8,
        "phase": phase,
        "new_submission_engine": "langgraph_v3",
        "legacy_rules": {
            "terminal": "read_only",
            "pristine_queued": "migrate_two_phase",
            "running": "drain_allowlist",
            "waiting": "drain_allowlist",
        },
        "drain_deadline": now + timedelta(days=1),
        "rollback_deadline": now + timedelta(days=2),
        "operator_approval_ref": "operator-approval:test-cutover",
    }


def _legacy_store(tmp_path, statuses: tuple[str, ...]) -> SQLiteWorkflowStore:
    store = SQLiteWorkflowStore(tmp_path / "legacy.sqlite3")
    service = WorkflowService(
        store=store,
        definitions=default_workflow_definitions(),
    )
    identity = RequestIdentity.for_user(
        user_id="cutover-user",
        agent_id="cutover-agent",
        session_id="cutover-session",
    )
    for index, status in enumerate(statuses):
        bundle = service.submit(
            identity=identity,
            ingress_run_id=f"legacy-run-{index}",
            submission=WorkflowSubmission(
                workflow_type="deep_research",
                objective=f"legacy objective {index}",
                deliverables=["research_report"],
                durability_reasons=["legacy_inventory_fixture"],
                idempotency_key=f"legacy-submission-{index}",
            ),
        )
        if status == "queued":
            continue
        changed = bundle.model_copy(deep=True)
        changed.workflow.status = status
        changed.workflow.phase = (
            "waiting_input"
            if status in {"waiting_input", "blocked"}
            else "executing"
            if status in {"running", "recovering"}
            else status
        )
        if status in {"completed", "failed", "cancelled"}:
            changed.workflow.terminal_at = datetime.now(timezone.utc)
        store.save(
            changed,
            expected_revision=bundle.workflow.revision,
            events=[],
        )
    return store


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retirement_gate_requires_persisted_manifest_bound_audit(
    tmp_path,
    backend: str,
) -> None:
    """A caller-supplied boolean must never substitute for a persisted audit fact."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    store = (
        InMemoryWorkflowStore()
        if backend == "memory"
        else SQLiteWorkflowStore(tmp_path / "retirement.sqlite3")
    )
    now = datetime(2030, 1, 10, tzinfo=timezone.utc)
    manifest = WorkflowEngineCutoverManifest.model_validate(
        _manifest_payload(phase="retired")
    )
    try:
        controller = WorkflowCutoverController(store=store, manifest=manifest)
        before = controller.retirement_status(now=now)
        assert before.ready is False
        assert before.retirement_audit_present is False
        assert before.reason_codes == ("retirement_audit_missing",)

        controller.record_retirement_audit(now=now)
        after = controller.retirement_status(now=now)
        assert after.ready is True
        assert after.nonterminal_legacy_count == 0
        assert after.active_legacy_lease_count == 0
        assert after.waiting_legacy_count == 0
        assert after.manifest_phase == "retired"
        assert after.manifest_revision == manifest.revision
        assert after.manifest_digest == manifest.digest
        assert after.rollback_closed is True
        assert after.retirement_audit_present is True
        assert after.reason_codes == ()
        persisted = store.list_retirement_audits()
        assert len(persisted) == 1
        assert persisted[0].manifest_revision == manifest.revision
        assert persisted[0].manifest_digest == manifest.digest
        assert persisted[0].operator_approval_ref == manifest.operator_approval_ref
    finally:
        store.close()

    if backend == "sqlite":
        reopened = SQLiteWorkflowStore(tmp_path / "retirement.sqlite3")
        try:
            status = WorkflowCutoverController(
                store=reopened,
                manifest=manifest,
            ).retirement_status(now=now)
            assert status.ready is True
            assert status.retirement_audit_present is True
        finally:
            reopened.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retirement_gate_reports_every_unmet_fact_without_content(
    tmp_path,
    backend: str,
) -> None:
    """Dropping any prerequisite must keep deletion fail-closed with safe codes."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    store = (
        InMemoryWorkflowStore()
        if backend == "memory"
        else SQLiteWorkflowStore(tmp_path / "blocked-retirement.sqlite3")
    )
    service = WorkflowService(store=store, definitions=default_workflow_definitions())
    identity = RequestIdentity.for_user(
        user_id="retirement-secret-user",
        agent_id="retirement-secret-agent",
        session_id="retirement-secret-session",
    )
    try:
        for index in range(2):
            service.submit(
                identity=identity,
                ingress_run_id=f"retirement-secret-run-{index}",
                submission=WorkflowSubmission(
                    workflow_type="deep_research",
                    objective=f"retirement secret objective {index}",
                    deliverables=["research_report"],
                    durability_reasons=["retirement_fixture"],
                    idempotency_key=f"retirement-secret-key-{index}",
                ),
            )
        ids = [bundle.workflow.workflow_id for bundle in store.list_cutover_bundles()]
        lease_now = datetime.now(timezone.utc)
        active = store.load(ids[0])
        assert active is not None
        active_item = active.current_plan.work_items[0]
        active_item.status = "running"
        active_item.active_attempt_id = "retired-attempt"
        active_item.lease_owner = "retired-worker"
        active_item.lease_token = "retired-lease"
        active_item.lease_expires_at = lease_now + timedelta(seconds=600)
        active.workflow.status = "running"
        active.workflow.phase = "executing"
        store.save(
            active,
            expected_revision=active.workflow.revision,
            events=[],
        )
        waiting = store.load(ids[1])
        assert waiting is not None
        waiting.workflow.status = "waiting_input"
        waiting.workflow.phase = "waiting_input"
        waiting.workflow.waiting_input = {"prompt": "retirement secret prompt"}
        store.save(
            waiting,
            expected_revision=waiting.workflow.revision,
            events=[],
        )

        status = WorkflowCutoverController(
            store=store,
            manifest=WorkflowEngineCutoverManifest.model_validate(
                _manifest_payload(phase="cutover_active")
            ),
        ).retirement_status(now=lease_now)
        assert status.ready is False
        assert status.nonterminal_legacy_count == 2
        assert status.active_legacy_lease_count == 1
        assert status.waiting_legacy_count == 1
        assert status.rollback_closed is False
        assert status.retirement_audit_present is False
        assert status.reason_codes == (
            "nonterminal_legacy_present",
            "active_legacy_leases_present",
            "waiting_legacy_present",
            "manifest_not_retired",
            "rollback_window_open",
            "retirement_audit_missing",
        )
        expired_status = WorkflowCutoverController(
            store=store,
            manifest=WorkflowEngineCutoverManifest.model_validate(
                _manifest_payload(phase="cutover_active")
            ),
        ).retirement_status(now=active_item.lease_expires_at)
        assert expired_status.active_legacy_lease_count == 0
        assert "active_legacy_leases_present" not in expired_status.reason_codes
        rendered = status.model_dump_json()
        assert "retirement secret" not in rendered
        assert not any(workflow_id in rendered for workflow_id in ids)
        with pytest.raises(
            ValueError,
            match="workflow_retirement_prerequisites_not_satisfied",
        ):
            WorkflowCutoverController(
                store=store,
                manifest=WorkflowEngineCutoverManifest.model_validate(
                    _manifest_payload(phase="cutover_active")
                ),
            ).record_retirement_audit(now=lease_now)
        assert store.list_retirement_audits() == []
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retirement_audit_rejects_a_different_manifest(
    tmp_path,
    backend: str,
) -> None:
    """Revising the manifest must not inherit or replace an older approval."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    store = (
        InMemoryWorkflowStore()
        if backend == "memory"
        else SQLiteWorkflowStore(tmp_path / "audit-conflict.sqlite3")
    )
    now = datetime(2030, 1, 10, tzinfo=timezone.utc)
    first = WorkflowEngineCutoverManifest.model_validate(
        _manifest_payload(phase="retired")
    )
    revised_payload = _manifest_payload(phase="retired")
    revised_payload["revision"] = first.revision + 1
    revised_payload["operator_approval_ref"] = "operator-approval:revised"
    revised = WorkflowEngineCutoverManifest.model_validate(revised_payload)
    try:
        first_controller = WorkflowCutoverController(store=store, manifest=first)
        first_audit = first_controller.record_retirement_audit(now=now)
        assert first_controller.record_retirement_audit(now=now) == first_audit

        revised_controller = WorkflowCutoverController(store=store, manifest=revised)
        status = revised_controller.retirement_status(now=now)
        assert status.ready is False
        assert status.retirement_audit_present is False
        assert status.reason_codes == ("retirement_audit_manifest_mismatch",)
        with pytest.raises(ValueError, match="retirement_audit_manifest_conflict"):
            revised_controller.record_retirement_audit(now=now)
        assert store.list_retirement_audits() == [first_audit]
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retirement_audit_requires_exact_operator_approval_binding(
    tmp_path,
    backend: str,
) -> None:
    """A forged approval ref cannot satisfy an otherwise matching audit key."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )
    from assistant_agent.workflows.models import WorkflowRetirementAudit

    store = (
        InMemoryWorkflowStore()
        if backend == "memory"
        else SQLiteWorkflowStore(tmp_path / "audit-approval-mismatch.sqlite3")
    )
    now = datetime(2030, 1, 10, tzinfo=timezone.utc)
    manifest = WorkflowEngineCutoverManifest.model_validate(
        _manifest_payload(phase="retired")
    )
    try:
        forged = WorkflowRetirementAudit(
            manifest_revision=manifest.revision,
            manifest_digest=manifest.digest,
            operator_approval_ref="operator-approval:forged",
            created_at=now,
        )
        store.record_retirement_audit(forged)
        controller = WorkflowCutoverController(store=store, manifest=manifest)
        status = controller.retirement_status(now=now)
        assert status.ready is False
        assert status.retirement_audit_present is False
        assert status.reason_codes == ("retirement_audit_manifest_mismatch",)
        with pytest.raises(ValueError, match="retirement_audit_manifest_conflict"):
            controller.record_retirement_audit(now=now)
        assert store.list_retirement_audits() == [forged]
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_concurrent_retirement_audit_callers_return_the_persisted_fact(
    tmp_path,
    backend: str,
) -> None:
    """A losing same-manifest writer must return the winner's stored timestamp."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    underlying = (
        InMemoryWorkflowStore()
        if backend == "memory"
        else SQLiteWorkflowStore(tmp_path / "audit-race.sqlite3")
    )
    barrier = Barrier(2)

    class RacingStore:
        def __getattr__(self, name):
            return getattr(underlying, name)

        def record_retirement_audit(self, audit):
            barrier.wait(timeout=5)
            return underlying.record_retirement_audit(audit)

    store = RacingStore()
    manifest = WorkflowEngineCutoverManifest.model_validate(
        _manifest_payload(phase="retired")
    )
    callers = (
        WorkflowCutoverController(store=store, manifest=manifest),
        WorkflowCutoverController(store=store, manifest=manifest),
    )
    timestamps = (
        datetime(2030, 1, 10, tzinfo=timezone.utc),
        datetime(2030, 1, 11, tzinfo=timezone.utc),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(controller.record_retirement_audit, now=now)
                for controller, now in zip(callers, timestamps, strict=True)
            ]
            returned = [future.result(timeout=5) for future in futures]
        persisted = underlying.list_retirement_audits()
        assert len(persisted) == 1
        assert returned == [persisted[0], persisted[0]]
    finally:
        underlying.close()


def test_sqlite_retirement_probe_opens_existing_database_read_only(tmp_path) -> None:
    """The machine gate must not initialize or mutate the operator database."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )
    from assistant_agent.workflows.models import WorkflowRetirementAudit
    from assistant_agent.workflows.store import WorkflowStoreError

    path = tmp_path / "read-only-retirement.sqlite3"
    writable = _legacy_store(tmp_path, ("running", "waiting_input"))
    writable_path = writable.path
    writable.close()
    assert writable_path != path
    writable_path.rename(path)
    before = path.stat()

    store = SQLiteWorkflowStore.open_read_only(path)
    try:
        status = WorkflowCutoverController(
            store=store,
            manifest=WorkflowEngineCutoverManifest.model_validate(
                _manifest_payload(phase="cutover_active")
            ),
        ).retirement_status(now=datetime.now(timezone.utc))
        assert status.ready is False
        assert status.nonterminal_legacy_count == 2
        assert status.waiting_legacy_count == 1
        with pytest.raises(WorkflowStoreError, match="workflow_store_read_only"):
            store.record_retirement_audit(
                WorkflowRetirementAudit(
                    manifest_revision=7,
                    manifest_digest="sha256:" + "0" * 64,
                    operator_approval_ref="operator-approval:read-only-test",
                    created_at=datetime.now(timezone.utc),
                )
            )
    finally:
        store.close()

    after = path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns


def test_cutover_manifest_is_strict_versioned_and_digestable() -> None:
    """An invalid operator phase or extra deployment fact must fail closed."""

    from assistant_agent.workflows.cutover import WorkflowEngineCutoverManifest

    manifest = WorkflowEngineCutoverManifest.model_validate(_manifest_payload())
    assert manifest.phase == "cutover_active"
    assert manifest.digest.startswith("sha256:")
    assert len(manifest.digest) == 71

    with pytest.raises(ValueError):
        WorkflowEngineCutoverManifest.model_validate(
            _manifest_payload(phase="unapproved")
        )
    with pytest.raises(ValueError):
        WorkflowEngineCutoverManifest.model_validate(
            {**_manifest_payload(), "deployment_secret": "must-not-be-accepted"}
        )


def test_cutover_inventory_classifies_every_legacy_status_without_user_text(
    tmp_path,
) -> None:
    """Adding a legacy status without a cutover class must break the inventory gate."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    store = _legacy_store(
        tmp_path,
        (
            "completed",
            "failed",
            "cancelled",
            "queued",
            "running",
            "recovering",
            "waiting_input",
            "blocked",
        ),
    )
    try:
        controller = WorkflowCutoverController(
            store=store,
            manifest=WorkflowEngineCutoverManifest.model_validate(_manifest_payload()),
        )
        inventory = controller.inventory()
        assert inventory.counts == {
            "terminal_read_only": 3,
            "migrate_pristine_queued": 1,
            "drain_running": 2,
            "drain_waiting": 2,
        }
        assert inventory.nonterminal_legacy_count == 5
        public = inventory.model_dump(mode="json")
        assert "legacy objective" not in str(public)
        assert "cutover-user" not in str(public)
    finally:
        store.close()


def test_legacy_bundle_without_display_title_remains_readable_for_retirement_inventory(
    tmp_path,
) -> None:
    """Pre-display-title rows must be projected safely without rewriting source text."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    store = _legacy_store(tmp_path, ("running",))
    try:
        current = store.list_cutover_bundles()[0]
        original = current.current_plan.work_items[0]
        raw = json.loads(current.model_dump_json())
        raw["workflow"].pop("execution_engine")
        for plan in raw["plans"]:
            for item in plan["work_items"]:
                item.pop("display_title")
        orphaned = raw["plans"][0]["work_items"][0]
        orphaned["status"] = "running"
        orphaned["attempt_count"] = 1
        for field in (
            "active_attempt_id",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
        ):
            orphaned[field] = None
        store._connection.execute(  # noqa: SLF001 - historical DB fixture
            "UPDATE durable_workflows SET bundle_json=? WHERE workflow_id=?",
            (json.dumps(raw), current.workflow.workflow_id),
        )
        store._connection.commit()  # noqa: SLF001 - historical DB fixture

        loaded = store.list_cutover_bundles()[0]
        migrated = loaded.current_plan.work_items[0]
        assert loaded.workflow.execution_engine == "legacy_scheduler_v2"
        assert migrated.display_title == "Legacy workflow step"
        assert migrated.work_item_id == original.work_item_id
        assert migrated.kind == original.kind
        assert migrated.objective == original.objective
        assert migrated.status == "retryable_failed"
        assert migrated.error_code == "legacy_orphaned_running_attempt"

        manifest = WorkflowEngineCutoverManifest.model_validate(_manifest_payload())
        controller = WorkflowCutoverController(store=store, manifest=manifest)
        assert controller.inventory().counts == {
            "terminal_read_only": 0,
            "migrate_pristine_queued": 0,
            "drain_running": 1,
            "drain_waiting": 0,
        }
    finally:
        store.close()


class _CheckpointHost:
    def __init__(self) -> None:
        import asyncio

        self.checkpoints: set[str] = set()
        self.start_counts: dict[str, int] = {}
        self.activation_counts: dict[str, int] = {}
        self._migration_lock = asyncio.Lock()

    @asynccontextmanager
    async def migration_guard(self, *, workflow_id: str):
        _ = workflow_id
        async with self._migration_lock:
            yield

    async def has_checkpoint(self, *, workflow_id: str) -> bool:
        return workflow_id in self.checkpoints

    async def ensure_started(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
    ) -> None:
        if workflow_id not in self.checkpoints:
            self.checkpoints.add(workflow_id)
            self.start_counts[workflow_id] = self.start_counts.get(workflow_id, 0) + 1

    async def activate(self, *, workflow_id: str) -> None:
        self.activation_counts[workflow_id] = (
            self.activation_counts.get(workflow_id, 0) + 1
        )


@pytest.mark.parametrize(
    "crash_point",
    ["after_prepare", "after_checkpoint", "before_commit"],
)
def test_pristine_queued_migration_reconciles_every_crash_point_once(
    tmp_path,
    crash_point: str,
) -> None:
    """Losing either side of the DB/checkpoint boundary must not duplicate start."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
        WorkflowMigrationReconciler,
    )

    async def exercise() -> None:
        store = _legacy_store(tmp_path, ("queued",))
        host = _CheckpointHost()
        try:
            manifest = WorkflowEngineCutoverManifest.model_validate(_manifest_payload())
            controller = WorkflowCutoverController(store=store, manifest=manifest)
            workflow_id = store.list_cutover_bundles()[0].workflow.workflow_id
            prepared = controller.prepare_pristine_queued(workflow_id)
            if crash_point in {"after_checkpoint", "before_commit"}:
                await host.ensure_started(
                    workflow_id=workflow_id,
                    idempotency_key=prepared.workflow.engine_migration.idempotency_key,
                )

            rebuilt = WorkflowMigrationReconciler(
                controller=WorkflowCutoverController(store=store, manifest=manifest),
                graph_host=host,
            )
            assert await rebuilt.reconcile_one(workflow_id) == "migration_committed"
            assert await rebuilt.reconcile_one(workflow_id) == "already_committed"
            record = store.load(workflow_id).workflow
            assert record.execution_engine == "langgraph_v3"
            assert record.engine_migration.status == "committed"
            assert host.start_counts[workflow_id] == 1
            assert host.activation_counts[workflow_id] == 1
            assert store.list_events(workflow_id)[-1].event_type == (
                "workflow.engine.migration_committed"
            )
        finally:
            store.close()

    import asyncio

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("cutover_active", "migration_committed"),
        ("draining", "migration_committed"),
        ("rollback_requested", "migration_rolled_back"),
    ],
)
def test_prepared_without_checkpoint_obeys_fresh_manifest_phase(
    tmp_path,
    phase: str,
    expected: str,
) -> None:
    """Using a stale phase could run Graph and rollback the same prepared row."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
        WorkflowMigrationReconciler,
    )

    async def exercise() -> None:
        store = _legacy_store(tmp_path, ("queued",))
        host = _CheckpointHost()
        try:
            initial = WorkflowEngineCutoverManifest.model_validate(_manifest_payload())
            workflow_id = store.list_cutover_bundles()[0].workflow.workflow_id
            WorkflowCutoverController(
                store=store,
                manifest=initial,
            ).prepare_pristine_queued(workflow_id)
            current = WorkflowEngineCutoverManifest.model_validate(
                _manifest_payload(phase=phase)
            )
            reconciler = WorkflowMigrationReconciler(
                controller=WorkflowCutoverController(store=store, manifest=current),
                graph_host=host,
            )
            assert await reconciler.reconcile_one(workflow_id) == expected
            record = store.load(workflow_id).workflow
            if expected == "migration_rolled_back":
                assert record.execution_engine == "legacy_scheduler_v2"
                assert record.engine_migration.status == "rolled_back"
                assert host.start_counts == {}
            else:
                assert record.execution_engine == "langgraph_v3"
                assert host.start_counts[workflow_id] == 1
        finally:
            store.close()

    import asyncio

    asyncio.run(exercise())


def test_direct_rollback_fails_closed_when_graph_checkpoint_exists(tmp_path) -> None:
    """No caller may roll business ownership back after graph state exists."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    async def exercise() -> None:
        store = _legacy_store(tmp_path, ("queued",))
        host = _CheckpointHost()
        try:
            workflow_id = store.list_cutover_bundles()[0].workflow.workflow_id
            WorkflowCutoverController(
                store=store,
                manifest=WorkflowEngineCutoverManifest.model_validate(
                    _manifest_payload(phase="cutover_active")
                ),
            ).prepare_pristine_queued(workflow_id)
            controller = WorkflowCutoverController(
                store=store,
                manifest=WorkflowEngineCutoverManifest.model_validate(
                    _manifest_payload(phase="rollback_requested")
                ),
            )
            host.checkpoints.add(workflow_id)
            with pytest.raises(ValueError, match="checkpoint"):
                await controller.rollback_prepared(
                    workflow_id,
                    graph_host=host,
                    now=datetime(2030, 1, 1, tzinfo=timezone.utc),
                )
            assert (
                store.load(workflow_id).workflow.engine_migration.status == "prepared"
            )
        finally:
            store.close()

    import asyncio

    asyncio.run(exercise())


def test_direct_commit_requires_matching_graph_checkpoint(tmp_path) -> None:
    """Transaction B cannot switch engine without current checkpoint proof."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    async def exercise() -> None:
        store = _legacy_store(tmp_path, ("queued",))
        host = _CheckpointHost()
        try:
            controller = WorkflowCutoverController(
                store=store,
                manifest=WorkflowEngineCutoverManifest.model_validate(
                    _manifest_payload()
                ),
            )
            workflow_id = store.list_cutover_bundles()[0].workflow.workflow_id
            controller.prepare_pristine_queued(workflow_id)
            with pytest.raises(ValueError, match="checkpoint proof"):
                await controller.commit_prepared(workflow_id, graph_host=host)
            assert store.load(workflow_id).workflow.execution_engine == (
                "legacy_scheduler_v2"
            )
        finally:
            store.close()

    import asyncio

    asyncio.run(exercise())


def test_reconciler_refreshes_manifest_before_no_checkpoint_decision(tmp_path) -> None:
    """One long-lived reconciler must observe an operator phase revision change."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
        WorkflowMigrationReconciler,
    )

    async def exercise() -> None:
        store = _legacy_store(tmp_path, ("queued",))
        host = _CheckpointHost()
        current = WorkflowEngineCutoverManifest.model_validate(_manifest_payload())

        def source():
            return current

        try:
            controller = WorkflowCutoverController(
                store=store,
                manifest=current,
                manifest_source=source,
            )
            workflow_id = store.list_cutover_bundles()[0].workflow.workflow_id
            controller.prepare_pristine_queued(workflow_id)
            current = WorkflowEngineCutoverManifest.model_validate(
                _manifest_payload(phase="rollback_requested")
            )
            reconciler = WorkflowMigrationReconciler(
                controller=controller,
                graph_host=host,
            )
            assert await reconciler.reconcile_one(workflow_id) == (
                "migration_rolled_back"
            )
            assert host.start_counts == {}
        finally:
            store.close()

    import asyncio

    asyncio.run(exercise())


def test_manifest_same_revision_is_immutable(tmp_path) -> None:
    """Operator phase changes must advance revision instead of rewriting history."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )

    store = _legacy_store(tmp_path, ())
    initial = WorkflowEngineCutoverManifest.model_validate(_manifest_payload())
    changed_payload = _manifest_payload(phase="rollback_requested")
    changed_payload["revision"] = initial.revision
    changed = WorkflowEngineCutoverManifest.model_validate(changed_payload)

    def source():
        return changed

    try:
        controller = WorkflowCutoverController(
            store=store,
            manifest=initial,
            manifest_source=source,
        )
        with pytest.raises(ValueError, match="same manifest revision"):
            controller.refresh_manifest()
    finally:
        store.close()
