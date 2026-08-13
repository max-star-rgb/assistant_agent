"""Process-owned production facade for native DurableWorkflowGraph."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.config import ProviderConfig
from assistant_agent.context.service import ContextService
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.checkpointer import AsyncCheckpointerOwner
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.artifacts import (
    ArtifactAccessDenied,
    ArtifactNotFound,
    LocalWorkflowArtifactStore,
)
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.durable_graph import build_durable_workflow_graph
from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionIdentity,
    WorkflowResume,
)
from assistant_agent.workflows.durable_graph_nodes import (
    WorkflowProfileBranchState,
    build_verifier_branch_subgraph,
    build_worker_branch_subgraph,
    verifier_child_runtime_context,
    worker_child_runtime_context,
)
from assistant_agent.workflows.graph_context import (
    BranchProfileContextFactory,
    WorkflowGraphRuntimeContext,
    WorkflowGraphRuntimeServices,
)
from assistant_agent.workflows.graph_projection import (
    WorkflowGraphProjector,
    WorkflowHandle,
    WorkflowProductEvent,
    WorkflowProductSnapshot,
)
from assistant_agent.workflows.graph_publish import (
    SQLiteWorkflowPublisher,
    SQLiteWorkflowPublishStore,
)
from assistant_agent.workflows.cutover import WorkflowEngineCutoverManifest
from assistant_agent.workflows.graph_state import (
    PersistedWorkflowIdentity,
    initial_workflow_graph_state,
    validate_durable_workflow_state,
    WorkflowGraphError,
)
from assistant_agent.workflows.models import (
    WorkflowBundle,
    WorkflowEvent,
    WorkflowSubmission,
    utc_now,
)
from assistant_agent.workflows.planning_graph import (
    build_workflow_planner_profile_graph,
    build_workflow_planning_subgraph,
)
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.store import WorkflowRevisionConflict


_PRODUCT_PROJECTION_EVENT = "graph_product_projection"
_SHUTDOWN_TIMEOUT_SECONDS = 30.0


class WorkflowGraphHostError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkflowGraphHandle(BaseModel):
    """Strict public submission result; native execution identity stays private."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_id: str
    workflow_type: str
    execution_engine: str
    status: str
    phase: str
    output_ref: str

    @classmethod
    def from_product(cls, handle: WorkflowHandle) -> "WorkflowGraphHandle":
        return cls(
            workflow_id=handle.workflow_id,
            workflow_type=handle.workflow_type,
            execution_engine="langgraph_v3",
            status=handle.status,
            phase=handle.phase,
            output_ref=handle.output_ref,
        )


class WorkflowGraphEventsPage(BaseModel):
    """Cursor page containing only strict committed product facts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_id: str
    events: tuple[WorkflowProductEvent, ...] = Field(default=(), max_length=500)
    next_cursor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_owner(self) -> "WorkflowGraphEventsPage":
        if any(event.workflow_id != self.workflow_id for event in self.events):
            raise ValueError("product event belongs to another workflow")
        return self


class WorkflowGraphResult(BaseModel):
    """Identity-scoped result artifact with no native execution identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_id: str
    artifact_ref: str
    content: str


class WorkflowGraphHost:
    """Own one official async saver, one compiled graph, and product services."""

    def __init__(
        self,
        *,
        config: ProviderConfig,
        owner: AsyncCheckpointerOwner,
        owns_owner: bool,
        graph_app: DurableWorkflowGraphApp,
        assistant_graph_app: AssistantTurnGraphApp,
        provider_registry: Any | None,
        tool_registry: ToolRegistry | None,
        product_store: SQLiteWorkflowStore,
        service: WorkflowService,
        artifact_store: LocalWorkflowArtifactStore,
        operation_store: SQLiteToolOperationStore,
        publish_store: SQLiteWorkflowPublishStore,
        publisher: SQLiteWorkflowPublisher,
        cutover_manifest_source: Callable[[], WorkflowEngineCutoverManifest] | None,
    ) -> None:
        self.config = config
        self._owner = owner
        self._owns_owner = owns_owner
        self._graph_app = graph_app
        self._assistant_graph_app = assistant_graph_app
        self._provider_registry = provider_registry
        self._tool_registry = tool_registry
        self._product_store = product_store
        self._service = service
        self._artifact_store = artifact_store
        self._operation_store = operation_store
        self._publish_store = publish_store
        self._publisher = publisher
        self._cutover_manifest_source = cutover_manifest_source
        self._cutover_manifest: WorkflowEngineCutoverManifest | None = None
        self._migration_local_lock = asyncio.Lock()
        self._migration_lock_path = Path(
            f"{config.langgraph_checkpoint_path}.workflow-migration.lock"
        )
        self._projector = WorkflowGraphProjector()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_errors: list[BaseException] = []
        self._schedule_lock = asyncio.Lock()
        self._activated_workflows: set[str] = set()
        self._cancel_tokens: dict[str, Event] = {}
        self._accepting = True
        self._closed = False

    @classmethod
    async def open(
        cls,
        *,
        config: ProviderConfig,
        provider_registry: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        checkpointer_owner: AsyncCheckpointerOwner | None = None,
        cutover_manifest_source: Callable[[], WorkflowEngineCutoverManifest] | None = None,
    ) -> "WorkflowGraphHost":
        if config.langgraph_checkpointer_backend != "sqlite":
            raise WorkflowGraphHostError(
                "workflow_persistent_saver_required",
                "Production WorkflowGraphHost requires the SQLite checkpointer.",
            )
        if tool_registry is not None and (
            not tool_registry.sealed or tool_registry.generation is None
        ):
            raise WorkflowGraphHostError(
                "workflow_tool_registry_unsealed",
                "WorkflowGraphHost requires a sealed Tool registry.",
            )
        owns_owner = checkpointer_owner is None
        owner = checkpointer_owner or AsyncCheckpointerOwner(config)
        if owns_owner:
            await owner.open()
        else:
            # Access validates that the process owner was opened before graph compile.
            owner.checkpointer
        product_store: SQLiteWorkflowStore | None = None
        artifact_store: LocalWorkflowArtifactStore | None = None
        try:
            product_store = SQLiteWorkflowStore(config.durable_workflow_path)
            artifact_store = LocalWorkflowArtifactStore(
                config.durable_workflow_artifact_path
            )
            business_path = Path(config.durable_workflow_path)
            operation_store = SQLiteToolOperationStore(
                business_path.with_name(f"{business_path.name}.operations.sqlite3")
            )
            publish_store = SQLiteWorkflowPublishStore(
                business_path.with_name(f"{business_path.name}.publish.sqlite3")
            )
            publisher = SQLiteWorkflowPublisher(
                business_path.with_name(f"{business_path.name}.publish-effects.sqlite3")
            )
            assistant_app = AssistantTurnGraphApp()
            planning = build_workflow_planning_subgraph(
                planner_graph=build_workflow_planner_profile_graph(
                    assistant_graph_app=assistant_app
                )
            )
            worker = build_worker_branch_subgraph(
                worker_graph=assistant_app.namespaced_graph_for_profile(
                    "worker",
                    state_schema=WorkflowProfileBranchState,
                    context_schema=WorkflowGraphRuntimeContext,
                    child_state_key="worker_child_state",
                    runtime_context_resolver=worker_child_runtime_context,
                )
            )
            verifier = build_verifier_branch_subgraph(
                verifier_graph=assistant_app.namespaced_graph_for_profile(
                    "verifier",
                    state_schema=WorkflowProfileBranchState,
                    context_schema=WorkflowGraphRuntimeContext,
                    child_state_key="verifier_child_state",
                    runtime_context_resolver=verifier_child_runtime_context,
                )
            )
            compiled = build_durable_workflow_graph(
                planning_subgraph=planning,
                worker_branch_subgraph=worker,
                verifier_branch_subgraph=verifier,
                checkpointer=owner.checkpointer,
            )
            return cls(
                config=config,
                owner=owner,
                owns_owner=owns_owner,
                graph_app=DurableWorkflowGraphApp(compiled),
                assistant_graph_app=assistant_app,
                provider_registry=provider_registry,
                tool_registry=tool_registry,
                product_store=product_store,
                service=WorkflowService(
                    store=product_store,
                    definitions=default_workflow_definitions(),
                    submission_engine="langgraph_v3",
                ),
                artifact_store=artifact_store,
                operation_store=operation_store,
                publish_store=publish_store,
                publisher=publisher,
                cutover_manifest_source=cutover_manifest_source,
            )
        except BaseException:
            if artifact_store is not None:
                artifact_store.close()
            if product_store is not None:
                product_store.close()
            if owns_owner:
                await owner.aclose()
            raise

    def bind_runtime_services(
        self,
        *,
        provider_registry: Any,
        tool_registry: ToolRegistry,
    ) -> None:
        """Bind shared Runtime services after both graphs use the process saver."""

        self._require_open()
        if not tool_registry.sealed or tool_registry.generation is None:
            raise WorkflowGraphHostError(
                "workflow_tool_registry_unsealed",
                "WorkflowGraphHost requires a sealed Tool registry.",
            )
        if self._provider_registry is not None or self._tool_registry is not None:
            if (
                self._provider_registry is provider_registry
                and self._tool_registry is tool_registry
            ):
                return
            raise WorkflowGraphHostError(
                "workflow_runtime_services_already_bound",
                "WorkflowGraphHost Runtime services are already bound.",
            )
        self._provider_registry = provider_registry
        self._tool_registry = tool_registry

    async def start(
        self,
        *,
        identity: RequestIdentity,
        ingress_run_id: str,
        submission: WorkflowSubmission,
        ingress_trace_id: str | None = None,
        ingress_parent_span_id: str | None = None,
    ) -> WorkflowGraphHandle:
        self._require_submission_allowed()
        bundle = self._service.submit(
            identity=identity,
            ingress_run_id=ingress_run_id,
            submission=submission,
            ingress_trace_id=ingress_trace_id,
            ingress_parent_span_id=ingress_parent_span_id,
        )
        workflow_id = bundle.workflow.workflow_id
        async with self._schedule_lock:
            existing = self._tasks.get(workflow_id)
            if existing is not None and not existing.done():
                return WorkflowGraphHandle.from_product(
                    (await self.get_status(
                        identity=identity,
                        workflow_id=workflow_id,
                    )).handle
                )
            if await self.has_checkpoint(workflow_id=workflow_id):
                return WorkflowGraphHandle.from_product(
                    (await self.get_status(
                        identity=identity,
                        workflow_id=workflow_id,
                    )).handle
                )
            graph_identity = self._execution_identity(
                bundle, run_id=f"workflow-start:{ingress_run_id}"
            )
            initial = initial_workflow_graph_state(
                workflow=bundle.workflow,
                submission=submission,
                admitted_plan=None,
                workflow_thread_id=graph_identity.thread_id,
                invocation_run_id=graph_identity.run_id,
                invocation_trace_id=ingress_trace_id or ingress_run_id,
            )
            context = self._context(
                bundle, invocation_token=_token(graph_identity.run_id)
            )
            await self._commit_projection(initial)
            self._track_task(
                workflow_id,
                self._run_initial(
                    initial,
                    identity=graph_identity,
                    context=context,
                ),
            )
        return WorkflowGraphHandle.from_product(
            self._projector.project_snapshot(initial).handle
        )

    async def recover_nonterminal(self) -> int:
        """Schedule every graph-owned nonterminal row after process restart."""

        self._require_accepting()
        recovered = 0
        for bundle in self._product_store.list_cutover_bundles():
            workflow = bundle.workflow
            if (
                workflow.execution_engine != "langgraph_v3"
                or workflow.status in {"completed", "failed", "cancelled"}
            ):
                continue
            workflow_id = workflow.workflow_id
            async with self._schedule_lock:
                existing = self._tasks.get(workflow_id)
                if existing is not None and not existing.done():
                    continue
                execution = self._execution_identity(
                    bundle, run_id="workflow-startup-recovery"
                )
                raw = await self._graph_app.graph.aget_state(
                    execution.runnable_config(), subgraphs=True
                )
                values = getattr(raw, "values", None)
                if not isinstance(values, dict) or values.get("graph_name") != (
                    "DurableWorkflowGraph"
                ):
                    initial = initial_workflow_graph_state(
                        workflow=workflow,
                        submission=_submission_from_bundle(bundle),
                        admitted_plan=None,
                        workflow_thread_id=execution.thread_id,
                        invocation_run_id=execution.run_id,
                        invocation_trace_id=workflow.ingress_trace_id
                        or workflow.ingress_run_id,
                    )
                    context = self._context(
                        bundle,
                        invocation_token=_token(initial["invocation_run_id"]),
                    )
                    await self._commit_projection(initial)
                    self._track_task(
                        workflow_id,
                        self._run_initial(
                            initial,
                            identity=execution,
                            context=context,
                        ),
                    )
                    recovered += 1
                    continue
                state = validate_durable_workflow_state(values)
                if state["status"] in {"completed", "failed", "cancelled"}:
                    await self._commit_projection(state)
                    continue
                if state["status"] in {"waiting_input", "blocked"}:
                    await self._commit_projection(state)
                    continue
                context = self._context(
                    bundle,
                    invocation_token=_token(state["invocation_run_id"]),
                )
                self._track_task(
                    workflow_id,
                    self._run_continue(identity=execution, context=context),
                )
                recovered += 1
        return recovered

    async def get_status(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
    ) -> WorkflowProductSnapshot:
        self._require_open()
        bundle = self._service.get_workflow(
            identity=identity,
            workflow_id=workflow_id,
        )
        execution = self._execution_identity(
            bundle, run_id="workflow-product-read"
        )
        raw = await self._graph_app.graph.aget_state(
            execution.runnable_config(), subgraphs=True
        )
        values = getattr(raw, "values", None)
        if not isinstance(values, dict) or values.get("graph_name") != (
            "DurableWorkflowGraph"
        ):
            values = initial_workflow_graph_state(
                workflow=bundle.workflow,
                submission=_submission_from_bundle(bundle),
                admitted_plan=None,
                workflow_thread_id=execution.thread_id,
                invocation_run_id=execution.run_id,
                invocation_trace_id=bundle.workflow.ingress_trace_id
                or bundle.workflow.ingress_run_id,
            )
        return self._projector.project_snapshot(values)

    async def get_events(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        after: int = 0,
        limit: int = 100,
    ) -> WorkflowGraphEventsPage:
        """Read committed graph product facts without native stream payloads."""

        self._require_open()
        self._service.get_workflow(identity=identity, workflow_id=workflow_id)
        bounded_limit = min(max(1, limit), 500)
        committed = self._product_store.list_events(
            workflow_id,
            after=max(0, after),
            limit=500,
        )
        product_events: list[WorkflowProductEvent] = []
        next_cursor = max(0, after)
        for item in committed:
            next_cursor = item.cursor
            if item.event_type != _PRODUCT_PROJECTION_EVENT:
                continue
            raw = item.payload.get("product_event")
            try:
                event = WorkflowProductEvent.model_validate_json(
                    json.dumps(raw, ensure_ascii=False, allow_nan=False)
                )
            except (TypeError, ValueError):
                continue
            product_events.append(event)
            if len(product_events) >= bounded_limit:
                break
        return WorkflowGraphEventsPage(
            workflow_id=workflow_id,
            events=tuple(product_events),
            next_cursor=next_cursor,
        )

    async def get_result(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
    ) -> WorkflowGraphResult:
        """Return the final owner-scoped artifact, failing closed if absent."""

        snapshot = await self.get_status(identity=identity, workflow_id=workflow_id)
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
            content = self._artifact_store.read_text(
                identity=identity,
                artifact_ref=artifact_ref,
            )
        except (ArtifactAccessDenied, ArtifactNotFound) as exc:
            raise WorkflowGraphHostError(
                "workflow_result_not_found", "Workflow result was not found."
            ) from exc
        return WorkflowGraphResult(
            workflow_id=workflow_id,
            artifact_ref=artifact_ref,
            content=content,
        )

    async def resume(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        action_ref: str,
        values: dict[str, str],
    ) -> WorkflowGraphHandle:
        """Register one product-ref resume and return the current accepted handle."""

        self._require_accepting()
        bundle = self._service.get_workflow(
            identity=identity,
            workflow_id=workflow_id,
        )
        if bundle.workflow.execution_engine != "langgraph_v3":
            raise WorkflowGraphHostError(
                "workflow_engine_not_graph",
                "Workflow is not owned by the graph execution engine.",
            )
        current = await self.get_status(identity=identity, workflow_id=workflow_id)
        if action_ref not in {item.action_ref for item in current.waiting_actions}:
            raise WorkflowGraphHostError(
                "workflow_resume_action_unknown",
                "Workflow action is not pending.",
            )
        run_id = "workflow-resume:" + secrets.token_hex(16)
        execution = self._execution_identity(bundle, run_id=run_id)
        context = self._context(bundle, invocation_token=_token(run_id))
        await self._wait_for_active_invocation(workflow_id)
        self._track_task(
            workflow_id,
            self._run_resume(
                identity=execution,
                context=context,
                resume=WorkflowResume(
                    values_by_action_ref={action_ref: dict(values)}
                ),
            ),
        )
        return WorkflowGraphHandle.from_product(current.handle)

    async def cancel(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        reason_code: str = "user_requested",
    ) -> WorkflowGraphHandle:
        """Persist one terminal product cancellation without exposing native tasks."""

        self._require_accepting()
        bundle = self._service.get_workflow(
            identity=identity,
            workflow_id=workflow_id,
        )
        if bundle.workflow.execution_engine != "langgraph_v3":
            raise WorkflowGraphHostError(
                "workflow_engine_not_graph",
                "Workflow is not owned by the graph execution engine.",
            )
        snapshot = await self.get_status(identity=identity, workflow_id=workflow_id)
        if snapshot.handle.status in {"completed", "failed", "cancelled"}:
            if snapshot.handle.status == "cancelled":
                return WorkflowGraphHandle.from_product(snapshot.handle)
            raise WorkflowGraphHostError(
                "workflow_terminal", "Terminal Workflow cannot be cancelled."
            )
        token = self._cancel_tokens.setdefault(workflow_id, Event())
        token.set()
        await self._wait_for_active_invocation(workflow_id)
        execution = self._execution_identity(
            bundle,
            run_id="workflow-cancel:" + secrets.token_hex(16),
        )
        raw = await self._graph_app.graph.aget_state(
            execution.runnable_config(), subgraphs=True
        )
        current = validate_durable_workflow_state(raw.values)
        await self._graph_app.graph.aupdate_state(
            execution.runnable_config(),
            {
                "status": "cancelled",
                "phase": "cancelled",
                "active_wave": (),
                "errors": (
                    WorkflowGraphError(
                        code=reason_code,
                        message="Workflow was cancelled by its owner.",
                    ),
                ),
            },
            as_node="fail",
        )
        updated = await self._graph_app.graph.aget_state(
            execution.runnable_config(), subgraphs=True
        )
        state = validate_durable_workflow_state(updated.values)
        if current["workflow_id"] != state["workflow_id"]:
            raise WorkflowGraphHostError(
                "workflow_cancel_identity_mismatch",
                "Workflow cancellation changed execution identity.",
            )
        await self._commit_projection(state)
        return WorkflowGraphHandle.from_product(
            self._projector.project_snapshot(state).handle
        )

    async def has_checkpoint(self, *, workflow_id: str) -> bool:
        self._require_open()
        bundle = self._require_bundle(workflow_id)
        snapshot = await self._graph_app.graph.aget_state(
            self._execution_identity(
                bundle, run_id="workflow-checkpoint-inspect"
            ).runnable_config()
        )
        values = getattr(snapshot, "values", None)
        if not isinstance(values, dict) or not values:
            return False
        try:
            state = validate_durable_workflow_state(values)
        except (TypeError, ValueError) as exc:
            raise WorkflowGraphHostError(
                "workflow_checkpoint_invalid",
                "Workflow checkpoint does not match the durable graph schema.",
            ) from exc
        expected_thread_id = _thread_id(workflow_id)
        if (
            state["workflow_id"] != workflow_id
            or state["workflow_thread_id"] != expected_thread_id
            or state["identity"]["workflow_thread_id"] != expected_thread_id
            or state["identity"]["user_id"] != bundle.workflow.user_id
            or state["identity"]["agent_id"] != bundle.workflow.agent_id
        ):
            raise WorkflowGraphHostError(
                "workflow_checkpoint_owner_mismatch",
                "Workflow checkpoint identity does not match the business owner.",
            )
        return True

    async def ensure_started(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
    ) -> None:
        """Idempotently create the initial checkpoint without executing a node."""

        self._require_accepting()
        async with self.migration_guard(workflow_id=workflow_id):
            await self._ensure_started_locked(
                workflow_id=workflow_id,
                idempotency_key=idempotency_key,
            )

    async def _ensure_started_locked(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
    ) -> None:
        bundle = self._require_bundle(workflow_id)
        migration = bundle.workflow.engine_migration
        if (
            migration is None
            or migration.status != "prepared"
            or migration.idempotency_key != idempotency_key
            or bundle.workflow.execution_engine != "legacy_scheduler_v2"
        ):
            raise WorkflowGraphHostError(
                "workflow_migration_prepare_invalid",
                "Workflow is not eligible for graph checkpoint preparation.",
            )
        if await self.has_checkpoint(workflow_id=workflow_id):
            return
        graph_record = bundle.workflow.model_copy(deep=True)
        graph_record.execution_engine = "langgraph_v3"
        graph_record.engine_migration = migration.model_copy(
            update={"status": "committed"}
        )
        graph_record.legacy_claim_frozen = False
        execution = self._execution_identity(
            bundle, run_id=f"workflow-migration:{idempotency_key}"
        )
        initial = initial_workflow_graph_state(
            workflow=graph_record,
            submission=_submission_from_bundle(bundle),
            admitted_plan=None,
            workflow_thread_id=execution.thread_id,
            invocation_run_id=execution.run_id,
            invocation_trace_id=bundle.workflow.ingress_trace_id
            or bundle.workflow.ingress_run_id,
        )
        await self._graph_app.graph.aupdate_state(
            execution.runnable_config(),
            initial,
            as_node="__start__",
        )
        if not await self.has_checkpoint(workflow_id=workflow_id):
            raise WorkflowGraphHostError(
                "workflow_migration_checkpoint_missing",
                "Workflow initial checkpoint was not persisted.",
            )

    @asynccontextmanager
    async def migration_guard(self, *, workflow_id: str) -> AsyncIterator[None]:
        """Serialize checkpoint/commit/rollback across hosts sharing the saver."""

        _ = workflow_id
        self._migration_lock_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._migration_local_lock:
            file_descriptor = os.open(
                self._migration_lock_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            try:
                await asyncio.to_thread(fcntl.flock, file_descriptor, fcntl.LOCK_EX)
                yield
            finally:
                await asyncio.to_thread(fcntl.flock, file_descriptor, fcntl.LOCK_UN)
                os.close(file_descriptor)

    async def activate(self, *, workflow_id: str) -> None:
        """Continue one committed checkpoint; repeated calls on this host are inert."""

        self._require_accepting()
        if workflow_id in self._activated_workflows:
            return
        bundle = self._require_bundle(workflow_id)
        migration = bundle.workflow.engine_migration
        if (
            bundle.workflow.execution_engine != "langgraph_v3"
            or migration is None
            or migration.status != "committed"
        ):
            raise WorkflowGraphHostError(
                "workflow_migration_not_committed",
                "Workflow cannot execute before business migration commit.",
            )
        execution = self._execution_identity(
            bundle,
            run_id=f"workflow-migration:{migration.idempotency_key}",
        )
        raw_snapshot = await self._graph_app.graph.aget_state(
            execution.runnable_config(), subgraphs=True
        )
        state = validate_durable_workflow_state(raw_snapshot.values)
        if state["status"] in {"completed", "failed", "cancelled"}:
            self._activated_workflows.add(workflow_id)
            return
        context = self._context(
            bundle,
            invocation_token=_token(state["invocation_run_id"]),
        )
        self._track_task(
            workflow_id,
            self._run_continue(
                identity=execution,
                context=context,
            ),
        )
        self._activated_workflows.add(workflow_id)

    def _track_task(self, workflow_id: str, awaitable: Any) -> None:
        current = self._tasks.get(workflow_id)
        if current is not None and not current.done():
            if hasattr(awaitable, "close"):
                awaitable.close()
            return
        task = asyncio.create_task(awaitable)
        self._tasks[workflow_id] = task

        def discard(completed: asyncio.Task[Any]) -> None:
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    self._task_errors.append(error)
            if self._tasks.get(workflow_id) is completed:
                self._tasks.pop(workflow_id, None)

        task.add_done_callback(discard)

    async def _wait_for_active_invocation(self, workflow_id: str) -> None:
        current = self._tasks.get(workflow_id)
        if current is not None and not current.done():
            await asyncio.shield(current)

    async def _run_initial(
        self,
        initial: dict[str, Any],
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
    ) -> None:
        result = await self._graph_app.arun(
            initial,
            identity=identity,
            context=context,
        )
        await self._commit_projection(result.final_state)

    async def _run_continue(
        self,
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
    ) -> None:
        result = await self._graph_app.acontinue(
            identity=identity,
            context=context,
        )
        await self._commit_projection(result.final_state)

    async def _run_resume(
        self,
        *,
        identity: WorkflowGraphExecutionIdentity,
        context: WorkflowGraphRuntimeContext,
        resume: WorkflowResume,
    ) -> None:
        result = await self._graph_app.aresume(
            identity=identity,
            context=context,
            resume=resume,
        )
        await self._commit_projection(result.final_state)

    async def _commit_projection(self, state: dict[str, Any]) -> None:
        """CAS one idempotent strict product event into business history."""

        event = self._projector.project_event(state)
        for _attempt in range(3):
            bundle = self._require_bundle(event.workflow_id)
            existing = self._product_store.list_events(
                event.workflow_id,
                after=0,
                limit=500,
            )
            if any(
                item.event_type == _PRODUCT_PROJECTION_EVENT
                and item.payload.get("product_event", {}).get("event_id")
                == event.event_id
                for item in existing
                if isinstance(item.payload.get("product_event"), dict)
            ):
                return
            updated = bundle.model_copy(deep=True)
            workflow = updated.workflow
            workflow.status = event.status
            workflow.phase = event.phase
            workflow.result_artifact_refs = list(event.result_artifact_refs)
            workflow.terminal_reason_code = event.terminal_reason_code
            workflow.waiting_input = (
                {
                    "actions": [
                        action.model_dump(mode="json")
                        for action in event.waiting_actions
                    ]
                }
                if event.waiting_actions
                else None
            )
            workflow.terminal_at = (
                utc_now()
                if event.status in {"completed", "failed", "cancelled"}
                else None
            )
            try:
                self._product_store.save(
                    updated,
                    expected_revision=bundle.workflow.revision,
                    events=[
                        WorkflowEvent(
                            workflow_id=event.workflow_id,
                            event_type=_PRODUCT_PROJECTION_EVENT,
                            status=event.status,
                            payload={"product_event": event.model_dump(mode="json")},
                        )
                    ],
                )
                return
            except WorkflowRevisionConflict:
                continue
        raise WorkflowGraphHostError(
            "workflow_projection_conflict",
            "Workflow product projection could not be committed.",
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._accepting = False
        tasks = tuple(self._tasks.values())
        if tasks:
            _done, pending = await asyncio.wait(
                tasks,
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)
        errors: list[BaseException] = list(self._task_errors)
        self._closed = True
        try:
            self._artifact_store.close()
            self._product_store.close()
        finally:
            if self._owns_owner:
                await self._owner.aclose()
        if errors:
            raise WorkflowGraphHostError(
                "workflow_background_execution_failed",
                "A Workflow graph invocation failed before host shutdown.",
            ) from errors[0]

    def _context(
        self,
        bundle: WorkflowBundle,
        *,
        invocation_token: str,
    ) -> WorkflowGraphRuntimeContext:
        self._require_runtime_services()
        workflow = bundle.workflow
        thread_id = _thread_id(workflow.workflow_id)
        persisted_identity = PersistedWorkflowIdentity(
            user_id=workflow.user_id,
            session_id=workflow.session_id,
            agent_id=workflow.agent_id,
            workflow_thread_id=thread_id,
            turn_origin_id=workflow.ingress_run_id,
        )
        services = WorkflowGraphRuntimeServices(
            provider_registry=self._provider_registry,
            tool_registry=self._tool_registry,
            context_service=ContextService(),
            operation_store=self._operation_store,
            workflow_identity=persisted_identity,
            cancel_reader=lambda assignment: self._cancel_tokens.get(
                assignment.workflow_id
            ),
            stream_writer=lambda _assignment, _fact: None,
            invocation_claim_store=self._owner.invocation_claim_store,
            publish_store=self._publish_store,
            publisher=self._publisher,
        )
        return WorkflowGraphRuntimeContext(
            assistant_graph_app=self._assistant_graph_app,
            artifact_store=self._artifact_store,
            context_compiler=WorkflowContextCompiler(
                artifact_store=self._artifact_store
            ),
            branch_context_factory=BranchProfileContextFactory(),
            services=services,
            invocation_token=invocation_token,
        )

    def _require_runtime_services(self) -> None:
        if self._provider_registry is None or self._tool_registry is None:
            raise WorkflowGraphHostError(
                "workflow_runtime_services_unbound",
                "WorkflowGraphHost Runtime services are not bound.",
            )

    def _require_bundle(self, workflow_id: str) -> WorkflowBundle:
        bundle = self._product_store.load(workflow_id)
        if bundle is None:
            raise WorkflowGraphHostError(
                "workflow_not_found", "Workflow was not found."
            )
        return bundle

    @staticmethod
    def _execution_identity(
        bundle: WorkflowBundle,
        *,
        run_id: str,
    ) -> WorkflowGraphExecutionIdentity:
        workflow = bundle.workflow
        return WorkflowGraphExecutionIdentity.for_workflow(
            workflow_id=workflow.workflow_id,
            workflow_thread_id=_thread_id(workflow.workflow_id),
            run_id=run_id,
            user_id=workflow.user_id,
            session_id=workflow.session_id,
            agent_id=workflow.agent_id,
        )

    def _require_accepting(self) -> None:
        self._require_open()
        self._require_runtime_services()
        if not self._accepting:
            raise WorkflowGraphHostError(
                "workflow_graph_host_not_accepting",
                "WorkflowGraphHost is not accepting new submissions.",
            )

    def _require_submission_allowed(self) -> None:
        self._require_accepting()
        source = self._cutover_manifest_source
        if source is None:
            return
        current = source()
        previous = self._cutover_manifest
        if previous is not None:
            if current.revision < previous.revision:
                raise WorkflowGraphHostError(
                    "workflow_cutover_manifest_stale",
                    "Workflow cutover manifest revision moved backwards.",
                )
            if current.revision == previous.revision and current.digest != previous.digest:
                raise WorkflowGraphHostError(
                    "workflow_cutover_manifest_conflict",
                    "Workflow cutover manifest revision is not immutable.",
                )
        self._cutover_manifest = current
        if current.phase == "rollback_requested":
            self._accepting = False
            raise WorkflowGraphHostError(
                "workflow_cutover_rollback_active",
                "Workflow Graph admission is stopped during operator rollback.",
            )

    def _require_open(self) -> None:
        if self._closed:
            raise WorkflowGraphHostError(
                "workflow_graph_host_closed", "WorkflowGraphHost is closed."
            )


def _thread_id(workflow_id: str) -> str:
    return f"workflow:{workflow_id}"


def _token(run_id: str) -> str:
    return "sha256:" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _submission_from_bundle(bundle: WorkflowBundle) -> WorkflowSubmission:
    workflow = bundle.workflow
    return WorkflowSubmission(
        workflow_type=workflow.workflow_type,
        objective=workflow.objective,
        deliverables=list(workflow.deliverables),
        constraints=list(workflow.constraints),
        inputs=dict(workflow.inputs),
        requested_budget={
            "model_calls": max(1, workflow.budget.model_calls_remaining),
            "tool_calls": max(1, workflow.budget.tool_calls_remaining),
            "workflow_quanta": max(1, workflow.budget.workflow_quanta_remaining),
            "deadline_seconds": max(
                60,
                int(
                    (
                        workflow.budget.deadline_at
                        - workflow.created_at
                    ).total_seconds()
                ),
            ),
        },
        durability_reasons=["legacy_cutover_migration"],
        seed_artifact_refs=list(workflow.seed_artifact_refs),
        idempotency_key=workflow.idempotency_key,
    )


__all__ = [
    "WorkflowGraphEventsPage",
    "WorkflowGraphHandle",
    "WorkflowGraphHost",
    "WorkflowGraphHostError",
    "WorkflowGraphResult",
]
