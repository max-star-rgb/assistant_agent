from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.config import ProviderConfig
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.models import WorkflowSubmission
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore


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
            manifest=WorkflowEngineCutoverManifest.model_validate(
                _manifest_payload()
            ),
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
        allowed = controller.legacy_drain_allowlist()
        assert {
            store.load(workflow_id).workflow.status for workflow_id in allowed
        } == {"running", "recovering", "waiting_input", "blocked"}
        assert len(allowed) == 4
    finally:
        store.close()


def test_legacy_bundle_without_display_title_remains_readable_for_drain_inventory(
    tmp_path,
) -> None:
    """Pre-display-title rows must be projected safely without rewriting source text."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )
    from assistant_agent.workflows.legacy_drain_host import LegacyDrainHost

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
        assert controller.legacy_drain_allowlist() == frozenset(
            {current.workflow.workflow_id}
        )
    finally:
        store.close()

    config = replace(
        ProviderConfig(),
        durable_workflow_path=str(tmp_path / "legacy.sqlite3"),
        durable_workflow_artifact_path=str(tmp_path / "artifacts"),
    )
    host = LegacyDrainHost.compose(
        config=config,
        agent_runtime=SimpleNamespace(context_token_counter=None),
        manifest=manifest,
    )
    try:
        assert host.allowed_workflow_ids == frozenset(
            {current.workflow.workflow_id}
        )
    finally:
        __import__("asyncio").run(host.close())


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
            assert store.load(workflow_id).workflow.engine_migration.status == "prepared"
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
