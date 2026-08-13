"""Cutover-only owner for an exact set of existing legacy Workflow rows."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.cutover import (
    WorkflowCutoverController,
    WorkflowEngineCutoverManifest,
    WorkflowMigrationGraphHost,
    WorkflowMigrationReconciler,
)
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.graph_host import (
    WorkflowGraphEventsPage,
    WorkflowGraphHandle,
    WorkflowGraphHostError,
    WorkflowGraphResult,
)
from assistant_agent.workflows.graph_projection import (
    WorkflowActiveItem,
    WorkflowHandle,
    WorkflowGraphProjector,
    WorkflowProductProgress,
    WorkflowProductSnapshot,
    WorkflowWaitingAction,
)
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.worker import DurableWorkflowWorker


class LegacyWorkflowHandle(BaseModel):
    """Faithful archived/legacy handle, separate from graph-only type constraints."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$")
    workflow_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    status: str
    phase: str
    output_ref: str


class LegacyWorkflowProductSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    handle: LegacyWorkflowHandle
    progress: WorkflowProductProgress
    result_artifact_refs: tuple[str, ...] = ()
    waiting_actions: tuple[WorkflowWaitingAction, ...] = ()
    terminal_reason_code: str | None = None


class LegacyDrainHost:
    """Own legacy execution only for a startup-frozen existing-row allowlist."""

    def __init__(
        self,
        *,
        service: WorkflowService,
        artifact_store: LocalWorkflowArtifactStore,
        worker: DurableWorkflowWorker | None,
        allowed_workflow_ids: frozenset[str],
    ) -> None:
        self.service = service
        self.artifact_store = artifact_store
        self.worker = worker
        self.allowed_workflow_ids = allowed_workflow_ids
        self.allowed_execution_engines = frozenset({"legacy_scheduler_v2"})
        self._stop_event: Event | None = None
        self._task: asyncio.Task[Any] | None = None
        self._closed = False
        self._projector = WorkflowGraphProjector()

    @classmethod
    def compose(
        cls,
        *,
        config: ProviderConfig,
        agent_runtime: Any,
        manifest: WorkflowEngineCutoverManifest,
    ) -> "LegacyDrainHost":
        store = SQLiteWorkflowStore(config.durable_workflow_path)
        artifact_store: LocalWorkflowArtifactStore | None = None
        try:
            service = WorkflowService(
                store=store,
                definitions=default_workflow_definitions(),
            )
            controller = WorkflowCutoverController(store=store, manifest=manifest)
            allowed_ids = controller.legacy_drain_allowlist()
            allowed_types = frozenset(
                bundle.workflow.workflow_type
                for bundle in store.list_cutover_bundles()
                if bundle.workflow.workflow_id in allowed_ids
            )
            artifact_store = LocalWorkflowArtifactStore(
                config.durable_workflow_artifact_path
            )
            worker = None
            if allowed_ids:
                executor = AgentRuntimeWorkItemExecutor(
                    agent_runtime=agent_runtime,
                    artifact_store=artifact_store,
                    context_compiler=WorkflowContextCompiler(
                        artifact_store=artifact_store,
                        token_counter=getattr(
                            agent_runtime, "context_token_counter", None
                        ),
                        model_context_window_tokens=(
                            config.context_input_token_limit
                        ),
                        output_reserve_tokens=config.deep_research_chat_max_tokens,
                        safety_margin_tokens=(
                            config.context_compaction_safety_margin_tokens
                        ),
                    ),
                    max_iterations=config.max_tool_iterations,
                )
                worker = DurableWorkflowWorker(
                    service=service,
                    runtime=WorkflowRuntime(
                        service=service,
                        work_item_executor=executor,
                        model_call_limit_per_item=config.max_tool_iterations,
                        tool_call_limit_per_item=max(
                            0, config.max_tool_iterations - 1
                        ),
                    ),
                    worker_id=(
                        f"api-legacy-drain-{os.getpid()}-{id(agent_runtime)}"
                    ),
                    lease_seconds=config.durable_workflow_lease_seconds,
                    poll_seconds=config.durable_workflow_poll_seconds,
                    max_concurrent_items=(
                        config.durable_workflow_max_concurrent_items
                    ),
                    allowed_execution_engines=frozenset(
                        {"legacy_scheduler_v2"}
                    ),
                    allowed_workflow_types=allowed_types,
                    allowed_workflow_ids=allowed_ids,
                )
            return cls(
                service=service,
                artifact_store=artifact_store,
                worker=worker,
                allowed_workflow_ids=allowed_ids,
            )
        except BaseException:
            if artifact_store is not None:
                artifact_store.close()
            store.close()
            raise

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("legacy drain host is closed")
        if self.worker is None or self._task is not None:
            return
        self._stop_event = Event()
        self._task = asyncio.create_task(
            asyncio.to_thread(self.worker.run, self._stop_event)
        )

    async def reconcile_pristine_migrations(
        self,
        *,
        graph_host: WorkflowMigrationGraphHost,
        manifest: WorkflowEngineCutoverManifest,
        manifest_source: Any,
    ) -> tuple[str, ...]:
        """Migrate existing pristine admissions before serving new requests."""

        controller = WorkflowCutoverController(
            store=self.service.store,
            manifest=manifest,
            manifest_source=manifest_source,
        )
        reconciler = WorkflowMigrationReconciler(
            controller=controller,
            graph_host=graph_host,
        )
        outcomes: list[str] = []
        for workflow_id in controller.pristine_migration_ids():
            bundle = self.service.store.load(workflow_id)
            if bundle is None:
                continue
            if bundle.workflow.engine_migration is None:
                controller.prepare_pristine_queued(workflow_id)
            outcomes.append(await reconciler.reconcile_one(workflow_id))
        return tuple(outcomes)

    def owns_legacy(self, *, identity: Any, workflow_id: str) -> bool:
        bundle = self.service.get_workflow(identity=identity, workflow_id=workflow_id)
        return bundle.workflow.execution_engine == "legacy_scheduler_v2"

    def get_status(
        self, *, identity: Any, workflow_id: str
    ) -> LegacyWorkflowProductSnapshot:
        bundle = self.service.get_workflow(identity=identity, workflow_id=workflow_id)
        workflow = bundle.workflow
        if workflow.execution_engine != "legacy_scheduler_v2":
            raise WorkflowGraphHostError(
                "workflow_engine_not_legacy", "Workflow is not owned by legacy drain."
            )
        waiting = workflow.status in {"waiting_input", "blocked"}
        terminal = workflow.status in {"completed", "failed", "cancelled"}
        phase = (
            workflow.status
            if terminal
            else "waiting_input"
            if waiting
            else "planning"
            if workflow.phase == "planning"
            else "executing"
        )
        active_items = tuple(
            WorkflowActiveItem(
                node_id=self._opaque_node_id(workflow.workflow_id, item.work_item_id),
                display_title=item.display_title,
                status=item.status,
                execution_generation=min(item.attempt_count, 64),
            )
            for item in bundle.current_plan.work_items
            if item.status in {"running", "blocked"} and not terminal
        )
        completed_items = sum(
            item.status in {"succeeded", "skipped", "superseded"}
            for item in bundle.current_plan.work_items
        )
        waiting_actions: tuple[WorkflowWaitingAction, ...] = ()
        if waiting:
            request = workflow.waiting_input
            blocked = next(
                (
                    item
                    for item in bundle.current_plan.work_items
                    if item.status == "blocked"
                ),
                None,
            )
            if not isinstance(request, dict) or blocked is None:
                raise WorkflowGraphHostError(
                    "workflow_resume_action_unknown",
                    "Legacy Workflow has no resumable product action.",
                )
            required = request.get("required_fields")
            if not isinstance(required, list) or not all(
                isinstance(item, str) for item in required
            ):
                raise WorkflowGraphHostError(
                    "workflow_resume_action_invalid",
                    "Legacy Workflow input contract is invalid.",
                )
            waiting_actions = (
                WorkflowWaitingAction(
                    action_ref=self._action_ref(bundle, blocked.work_item_id),
                    node_id=self._opaque_node_id(
                        workflow.workflow_id, blocked.work_item_id
                    ),
                    required_fields=tuple(required),
                    prompt_code=str(request.get("prompt_code") or "input_required"),
                    safe_prompt=str(
                        request.get("safe_prompt") or "Additional input is required."
                    ),
                ),
            )
        progress_state = (
            "waiting_input"
            if waiting
            else "completed"
            if workflow.status == "completed"
            else "failed"
            if workflow.status in {"failed", "cancelled"}
            else "planning"
            if phase == "planning"
            else "working"
        )
        reason = workflow.terminal_reason_code
        if workflow.status in {"failed", "cancelled"} and reason is None:
            reason = "legacy_workflow_failed"
        return LegacyWorkflowProductSnapshot(
            handle=LegacyWorkflowHandle(
                workflow_id=workflow.workflow_id,
                workflow_type=workflow.workflow_type,
                status=workflow.status,
                phase=phase,
                output_ref=f"workflow://{workflow.workflow_id}",
            ),
            progress=WorkflowProductProgress(
                state=progress_state,
                phase=phase,
                completed_items=completed_items,
                total_items=len(bundle.current_plan.work_items),
                active_items=active_items,
            ),
            result_artifact_refs=tuple(workflow.result_artifact_refs),
            waiting_actions=waiting_actions,
            terminal_reason_code=reason,
        )

    def resume(
        self,
        *,
        identity: Any,
        workflow_id: str,
        action_ref: str,
        values: dict[str, str],
    ) -> WorkflowGraphHandle:
        if workflow_id not in self.allowed_workflow_ids:
            raise WorkflowGraphHostError(
                "workflow_legacy_drain_forbidden",
                "Legacy Workflow is not in the frozen drain allowlist.",
            )
        bundle = self.service.get_workflow(identity=identity, workflow_id=workflow_id)
        blocked = next(
            (
                item
                for item in bundle.current_plan.work_items
                if item.status == "blocked"
                and hmac.compare_digest(
                    action_ref,
                    self._action_ref(bundle, item.work_item_id),
                )
            ),
            None,
        )
        if blocked is None:
            raise WorkflowGraphHostError(
                "workflow_resume_action_unknown", "Workflow action is not pending."
            )
        waiting = bundle.workflow.waiting_input
        resume_token = waiting.get("resume_token") if isinstance(waiting, dict) else None
        if not isinstance(resume_token, str):
            raise WorkflowGraphHostError(
                "workflow_resume_action_invalid",
                "Legacy Workflow resume state is unavailable.",
            )
        resumed = self.service.provide_input(
            identity=identity,
            workflow_id=workflow_id,
            resume_token=resume_token,
            values=dict(values),
        )
        return WorkflowGraphHandle(
            workflow_id=workflow_id,
            workflow_type=resumed.workflow.workflow_type,
            execution_engine="legacy_scheduler_v2",
            status=resumed.workflow.status,
            phase="executing",
            output_ref=f"workflow://{workflow_id}",
        )

    def cancel(
        self,
        *,
        identity: Any,
        workflow_id: str,
        reason_code: str,
    ) -> WorkflowGraphHandle:
        if workflow_id not in self.allowed_workflow_ids:
            raise WorkflowGraphHostError(
                "workflow_legacy_drain_forbidden",
                "Legacy Workflow is not in the frozen drain allowlist.",
            )
        bundle = self.service.cancel(
            identity=identity,
            workflow_id=workflow_id,
            reason_code=reason_code,
        )
        snapshot = self.get_status(identity=identity, workflow_id=workflow_id)
        return WorkflowGraphHandle(
            workflow_id=workflow_id,
            workflow_type=bundle.workflow.workflow_type,
            execution_engine="legacy_scheduler_v2",
            status=snapshot.handle.status,
            phase=snapshot.handle.phase,
            output_ref=f"workflow://{workflow_id}",
        )

    def get_events(
        self,
        *,
        identity: Any,
        workflow_id: str,
        after: int = 0,
        limit: int = 100,
    ) -> WorkflowGraphEventsPage:
        self.service.get_workflow(identity=identity, workflow_id=workflow_id)
        committed = self.service.list_events(
            identity=identity,
            workflow_id=workflow_id,
            after=after,
            limit=limit,
        )
        events = ()
        if committed:
            snapshot = self.get_status(identity=identity, workflow_id=workflow_id)
            graph_event_snapshot = WorkflowProductSnapshot(
                handle=WorkflowHandle(
                    workflow_id=snapshot.handle.workflow_id,
                    workflow_type="deep_research",
                    status=snapshot.handle.status,
                    phase=snapshot.handle.phase,
                    output_ref=snapshot.handle.output_ref,
                ),
                progress=snapshot.progress,
                result_artifact_refs=snapshot.result_artifact_refs,
                waiting_actions=snapshot.waiting_actions,
                terminal_reason_code=snapshot.terminal_reason_code,
            )
            events = (
                self._projector.project_snapshot_event(graph_event_snapshot),
            )
        return WorkflowGraphEventsPage(
            workflow_id=workflow_id,
            events=events,
            next_cursor=committed[-1].cursor if committed else max(0, after),
        )

    def get_result(
        self,
        *,
        identity: Any,
        workflow_id: str,
    ) -> WorkflowGraphResult:
        snapshot = self.get_status(identity=identity, workflow_id=workflow_id)
        if snapshot.handle.status != "completed":
            raise WorkflowGraphHostError(
                "workflow_result_not_ready", "Workflow result is not ready."
            )
        if not snapshot.result_artifact_refs:
            raise WorkflowGraphHostError(
                "workflow_result_not_found", "Workflow result was not found."
            )
        artifact_ref = snapshot.result_artifact_refs[-1]
        try:
            content = self.artifact_store.read_text(
                identity=identity,
                artifact_ref=artifact_ref,
            )
        except Exception as exc:
            raise WorkflowGraphHostError(
                "workflow_result_not_found", "Workflow result was not found."
            ) from exc
        return WorkflowGraphResult(
            workflow_id=workflow_id,
            artifact_ref=artifact_ref,
            content=content,
        )

    @staticmethod
    def _action_ref(bundle: Any, node_id: str) -> str:
        item = next(
            item
            for item in bundle.current_plan.work_items
            if item.work_item_id == node_id
        )
        opaque_node_id = LegacyDrainHost._opaque_node_id(
            bundle.workflow.workflow_id, node_id
        )
        return (
            f"workflow:{bundle.workflow.workflow_id}:node:{opaque_node_id}:generation:"
            f"{min(item.attempt_count, 64)}"
        )

    @staticmethod
    def _opaque_node_id(workflow_id: str, work_item_id: str) -> str:
        digest = hashlib.sha256(
            b"assistant-agent:legacy-workflow-action:v1\0"
            + workflow_id.encode("utf-8")
            + b"\0"
            + work_item_id.encode("utf-8")
        ).hexdigest()
        return f"legacy_{digest[:32]}"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self.artifact_store.close()
        self.service.store.close()


__all__ = ["LegacyDrainHost"]
