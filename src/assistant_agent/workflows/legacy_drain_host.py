"""Cutover-only owner for an exact set of existing legacy Workflow rows."""

from __future__ import annotations

import asyncio
import os
from threading import Event
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.cutover import (
    WorkflowCutoverController,
    WorkflowEngineCutoverManifest,
)
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.worker import DurableWorkflowWorker


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
