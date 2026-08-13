from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.models import WorkflowSubmission
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from tests.core.support import ProbeTool


class _Planner:
    provider = "scripted"
    model = "workflow-host-planner"

    def chat(self, _request):
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=json.dumps(
                {
                    "workflow_plan": {
                        "schema_version": "workflow_plan_v2",
                        "nodes": [
                            {
                                "node_id": "collect",
                                "display_title": "Collect",
                                "objective": "collect evidence",
                                "depends_on": [],
                                "acceptance_contract": {
                                    "schema_version": "workflow_step_acceptance_v2",
                                    "output": {
                                        "artifact_type": "research_report",
                                        "description": "Evidence",
                                    },
                                    "criteria": [
                                        {
                                            "criterion_id": "evidence",
                                            "statement": "Evidence exists",
                                        }
                                    ],
                                },
                            }
                        ],
                        "deliverable_bindings": [
                            {
                                "deliverable": "research_report",
                                "producer_node_id": "collect",
                            }
                        ],
                        "constraint_bindings": [],
                    }
                }
            ),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class _Worker:
    provider = "scripted"
    model = "workflow-host-worker"

    def chat(self, _request):
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=json.dumps(
                {
                    "workflow_control": {
                        "outcome": "completed",
                        "summary": "collected",
                    }
                }
            ),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class _CountingWorker(_Worker):
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, request):
        self.calls += 1
        return super().chat(request)


class _BlockingWorker:
    provider = "scripted"
    model = "workflow-host-blocking-worker"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, _request):
        self.calls += 1
        control = (
            {
                "outcome": "blocked",
                "summary": "answer required",
                "required_fields": ["answer"],
                "prompt_code": "answer_required",
                "safe_prompt": "Please provide the requested answer.",
            }
            if self.calls == 1
            else {"outcome": "completed", "summary": "answer accepted"}
        )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=json.dumps({"workflow_control": control}),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class _Verifier:
    provider = "scripted"
    model = "workflow-host-verifier"

    def chat(self, _request):
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=json.dumps(
                {"workflow_verification": {"status": "verified", "summary": "ok"}}
            ),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _config(tmp_path) -> ProviderConfig:
    return replace(
        ProviderConfig(),
        durable_workflows_enabled=True,
        durable_workflow_path=str(tmp_path / "products.sqlite3"),
        durable_workflow_artifact_path=str(tmp_path / "artifacts"),
        langgraph_checkpointer_backend="sqlite",
        langgraph_checkpoint_path=str(tmp_path / "checkpoints.sqlite3"),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ProbeTool())
    registry.seal()
    return registry


def _submission() -> WorkflowSubmission:
    return WorkflowSubmission(
        workflow_type="deep_research",
        objective="collect evidence",
        deliverables=["research_report"],
        inputs={"research_questions": ["collect evidence"], "source_target": 1},
        durability_reasons=["native_graph_host_test"],
        idempotency_key="host-submission-1",
    )


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="host-user",
        agent_id="host-agent",
        session_id="host-session",
    )


def _write_cutover_manifest(tmp_path) -> str:
    path = tmp_path / "operator-cutover.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_engine_cutover_v1",
                "revision": 7,
                "phase": "cutover_active",
                "new_submission_engine": "langgraph_v3",
                "legacy_rules": {
                    "terminal": "read_only",
                    "pristine_queued": "migrate_two_phase",
                    "running": "drain_allowlist",
                    "waiting": "drain_allowlist",
                },
                "drain_deadline": "2030-01-02T00:00:00Z",
                "rollback_deadline": "2030-01-03T00:00:00Z",
                "operator_approval_ref": "operator-approval:test-host",
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _forbidden_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_forbidden_keys(item) for item in value.values()),
            set(),
        )
    if isinstance(value, (list, tuple)):
        return set().union(*(_forbidden_keys(item) for item in value), set())
    return set()


def test_workflow_graph_host_reopens_persistent_thread_and_projects_strict_status(
    tmp_path,
) -> None:
    """A second host must recover the same product without native checkpoint IDs."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        providers = {
            "planner": _Planner(),
            "worker": _Worker(),
            "verifier": _Verifier(),
        }
        first = await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry=providers,
            tool_registry=_registry(),
        )
        try:
            handle = await first.start(
                identity=_identity(),
                ingress_run_id="host-run-1",
                submission=_submission(),
            )
            assert handle.execution_engine == "langgraph_v3"
            assert handle.output_ref == f"workflow://{handle.workflow_id}"
        finally:
            await first.close()

        second = await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry=providers,
            tool_registry=_registry(),
        )
        try:
            status = await second.get_status(
                identity=_identity(),
                workflow_id=handle.workflow_id,
            )
            assert status.handle.workflow_id == handle.workflow_id
            assert status.handle.status == "completed"
            assert status.handle.output_ref == handle.output_ref
            forbidden = {
                "checkpoint_id",
                "checkpoint_ns",
                "task_id",
                "native_interrupt_id",
                "thread_id",
                "invocation_run_id",
            }
            assert not (_forbidden_keys(status.model_dump(mode="json")) & forbidden)

            events = await second.get_events(
                identity=_identity(),
                workflow_id=handle.workflow_id,
            )
            assert events.events[-1].event_type == "completed"
            assert events.next_cursor >= 1
            assert not (
                _forbidden_keys(events.model_dump(mode="json")) & forbidden
            )

            with pytest.raises(Exception) as exc_info:
                await second.get_result(
                    identity=_identity(),
                    workflow_id=handle.workflow_id,
                )
            assert getattr(exc_info.value, "code", None) == (
                "workflow_result_not_found"
            )
        finally:
            await second.close()

    asyncio.run(exercise())


def test_real_host_migration_checkpoint_survives_reopen_before_commit(
    tmp_path,
) -> None:
    """The prepared/checkpoint crash gap must recover through the official saver."""

    from assistant_agent.workflows.cutover import (
        WorkflowCutoverController,
        WorkflowEngineCutoverManifest,
    )
    from assistant_agent.workflows.graph_host import WorkflowGraphHost, _token

    async def exercise() -> None:
        config = _config(tmp_path)
        seed_store = SQLiteWorkflowStore(config.durable_workflow_path)
        service = WorkflowService(
            store=seed_store,
            definitions=default_workflow_definitions(),
        )
        try:
            bundle = service.submit(
                identity=_identity(),
                ingress_run_id="legacy-before-cutover",
                submission=_submission(),
            )
        finally:
            seed_store.close()

        manifest = WorkflowEngineCutoverManifest.model_validate(
            {
                "schema_version": "workflow_engine_cutover_v1",
                "revision": 1,
                "phase": "cutover_active",
                "new_submission_engine": "langgraph_v3",
                "legacy_rules": {
                    "terminal": "read_only",
                    "pristine_queued": "migrate_two_phase",
                    "running": "drain_allowlist",
                    "waiting": "drain_allowlist",
                },
                "drain_deadline": datetime(2030, 1, 2, tzinfo=timezone.utc),
                "rollback_deadline": datetime(2030, 1, 3, tzinfo=timezone.utc),
                "operator_approval_ref": "operator-approval:test",
            }
        )
        controller_store = SQLiteWorkflowStore(config.durable_workflow_path)
        try:
            controller = WorkflowCutoverController(
                store=controller_store,
                manifest=manifest,
            )
            prepared = controller.prepare_pristine_queued(bundle.workflow.workflow_id)
            migration = prepared.workflow.engine_migration
            assert migration is not None

            first = await WorkflowGraphHost.open(
                config=config,
                provider_registry={
                    "planner": _Planner(),
                    "worker": _Worker(),
                    "verifier": _Verifier(),
                },
                tool_registry=_registry(),
            )
            try:
                await first.ensure_started(
                    workflow_id=bundle.workflow.workflow_id,
                    idempotency_key=migration.idempotency_key,
                )
                assert await first.has_checkpoint(
                    workflow_id=bundle.workflow.workflow_id
                )
            finally:
                await first.close()

            second = await WorkflowGraphHost.open(
                config=config,
                provider_registry={
                    "planner": _Planner(),
                    "worker": _Worker(),
                    "verifier": _Verifier(),
                },
                tool_registry=_registry(),
            )
            try:
                assert await second.has_checkpoint(
                    workflow_id=bundle.workflow.workflow_id
                )
                checkpoint = await second._graph_app.graph.aget_state(
                    second._execution_identity(
                        prepared,
                        run_id="migration-token-inspect",
                    ).runnable_config()
                )
                observed_tokens: list[str] = []
                real_context = second._context

                def observe_context(bundle, *, invocation_token):
                    observed_tokens.append(invocation_token)
                    return real_context(bundle, invocation_token=invocation_token)

                second._context = observe_context
                await controller.commit_prepared(
                    bundle.workflow.workflow_id,
                    graph_host=second,
                )
                await second.activate(workflow_id=bundle.workflow.workflow_id)
                assert observed_tokens == [
                    _token(checkpoint.values["invocation_run_id"])
                ]
            finally:
                await second.close()

            third = await WorkflowGraphHost.open(
                config=config,
                provider_registry={
                    "planner": _Planner(),
                    "worker": _Worker(),
                    "verifier": _Verifier(),
                },
                tool_registry=_registry(),
            )
            try:
                status = await third.get_status(
                    identity=_identity(), workflow_id=bundle.workflow.workflow_id
                )
                assert status.handle.status == "completed"
            finally:
                await third.close()
        finally:
            controller_store.close()

    asyncio.run(exercise())


def test_api_lifespan_opens_owner_before_both_graphs_and_closes_in_reverse(
    monkeypatch,
) -> None:
    """The process saver must exist before Workflow and Assistant compilation."""

    from assistant_agent.api import app as app_module

    calls: list[str] = []
    application = FastAPI()
    application.state.agent_runtime = object()

    def async_call(name: str):
        async def invoke(*_args, **_kwargs):
            calls.append(name)

        return invoke

    monkeypatch.setattr(
        app_module,
        "start_shared_checkpointer_owner",
        async_call("start_saver_owner"),
    )
    class RecoveringHost:
        async def recover_nonterminal(self) -> int:
            calls.append("recover_nonterminal")
            return 0

    async def start_graph_host(app, *_args, **_kwargs):
        calls.append("start_graph_host")
        app.state.workflow_graph_host = RecoveringHost()

    monkeypatch.setattr(app_module, "start_workflow_graph_host", start_graph_host)
    monkeypatch.setattr(
        app_module,
        "start_shared_agent_runtime",
        lambda *_args: calls.append("start_runtime"),
    )
    monkeypatch.setattr(
        app_module, "start_durable_task_worker", async_call("start_tasks")
    )
    monkeypatch.setattr(
        app_module, "start_durable_workflow_worker", async_call("start_legacy_drain")
    )
    monkeypatch.setattr(
        app_module, "shutdown_workflow_graph_host", async_call("stop_graph_host")
    )
    monkeypatch.setattr(
        app_module,
        "shutdown_durable_workflow_worker",
        async_call("stop_legacy_drain"),
    )
    monkeypatch.setattr(
        app_module, "shutdown_durable_task_worker", async_call("stop_tasks")
    )
    monkeypatch.setattr(
        app_module, "shutdown_gateway_runtime", async_call("stop_gateway")
    )
    monkeypatch.setattr(
        app_module,
        "shutdown_shared_agent_runtime",
        lambda *_args: calls.append("stop_runtime"),
    )
    monkeypatch.setattr(
        app_module,
        "shutdown_shared_checkpointer_owner",
        async_call("stop_saver_owner"),
    )
    monkeypatch.setattr(app_module, "prepare_server_startup_report", lambda *_args: None)

    async def exercise() -> None:
        async with app_module._lifespan(application):
            assert calls == [
                "start_saver_owner",
                "start_graph_host",
                "start_runtime",
                "start_tasks",
                "start_legacy_drain",
                "recover_nonterminal",
            ]

    asyncio.run(exercise())
    assert calls == [
        "start_saver_owner",
        "start_graph_host",
        "start_runtime",
        "start_tasks",
        "start_legacy_drain",
        "recover_nonterminal",
        "stop_graph_host",
        "stop_legacy_drain",
        "stop_tasks",
        "stop_gateway",
        "stop_runtime",
        "stop_saver_owner",
    ]


def test_duplicate_submission_coalesces_one_graph_execution(tmp_path) -> None:
    """A retried admission must not schedule the same product execution twice."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        worker = _CountingWorker()
        host = await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry={
                "planner": _Planner(),
                "worker": worker,
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )
        try:
            first = await host.start(
                identity=_identity(),
                ingress_run_id="duplicate-ingress",
                submission=_submission(),
            )
            second = await host.start(
                identity=_identity(),
                ingress_run_id="duplicate-ingress",
                submission=_submission(),
            )
            assert second.workflow_id == first.workflow_id
        finally:
            await host.close()
        assert worker.calls == 1

    asyncio.run(exercise())


def test_rollback_requested_manifest_fences_new_graph_admission(tmp_path) -> None:
    """Fresh operator rollback phase must stop new graph submissions first."""

    from assistant_agent.workflows.cutover import WorkflowEngineCutoverManifest
    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    manifest = WorkflowEngineCutoverManifest.model_validate(
        {
            "schema_version": "workflow_engine_cutover_v1",
            "revision": 8,
            "phase": "rollback_requested",
            "new_submission_engine": "langgraph_v3",
            "legacy_rules": {
                "terminal": "read_only",
                "pristine_queued": "migrate_two_phase",
                "running": "drain_allowlist",
                "waiting": "drain_allowlist",
            },
            "drain_deadline": datetime(2030, 1, 2, tzinfo=timezone.utc),
            "rollback_deadline": datetime(2030, 1, 3, tzinfo=timezone.utc),
            "operator_approval_ref": "operator-approval:rollback",
        }
    )

    async def exercise() -> None:
        host = await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry={
                "planner": _Planner(),
                "worker": _Worker(),
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
            cutover_manifest_source=lambda: manifest,
        )
        try:
            with pytest.raises(Exception) as exc_info:
                await host.start(
                    identity=_identity(),
                    ingress_run_id="fenced-during-rollback",
                    submission=_submission(),
                )
            assert getattr(exc_info.value, "code", None) == (
                "workflow_cutover_rollback_active"
            )
            assert host._product_store.list_cutover_bundles() == []
        finally:
            await host.close()

    asyncio.run(exercise())


def test_separate_hosts_serialize_migration_checkpoint_and_rollback(tmp_path) -> None:
    """The migration barrier must coordinate independent hosts, not only tasks."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        config = _config(tmp_path)
        first = await WorkflowGraphHost.open(config=config)
        second = await WorkflowGraphHost.open(config=config)
        entered = asyncio.Event()

        async def contender() -> None:
            async with second.migration_guard(workflow_id="workflow-lock-probe"):
                entered.set()

        try:
            async with first.migration_guard(workflow_id="workflow-lock-probe"):
                task = asyncio.create_task(contender())
                await asyncio.sleep(0.05)
                assert not entered.is_set()
            await asyncio.wait_for(task, timeout=1.0)
            assert entered.is_set()
        finally:
            await first.close()
            await second.close()

    asyncio.run(exercise())


def test_graph_host_close_cancels_after_bounded_drain(tmp_path, monkeypatch) -> None:
    """A stuck invocation cannot hold process shutdown indefinitely."""

    from assistant_agent.workflows import graph_host as graph_host_module

    async def exercise() -> None:
        host = await graph_host_module.WorkflowGraphHost.open(config=_config(tmp_path))
        never = asyncio.Event()
        task = asyncio.create_task(never.wait())
        host._tasks["stuck-workflow"] = task
        monkeypatch.setattr(graph_host_module, "_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
        await asyncio.wait_for(host.close(), timeout=0.5)
        assert task.cancelled()

    asyncio.run(exercise())


def test_recover_nonterminal_starts_graph_admission_without_checkpoint(tmp_path) -> None:
    """Startup recovery must execute an admitted graph row after a pre-checkpoint crash."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        config = _config(tmp_path)
        store = SQLiteWorkflowStore(config.durable_workflow_path)
        try:
            bundle = WorkflowService(
                store=store,
                definitions=default_workflow_definitions(),
                submission_engine="langgraph_v3",
            ).submit(
                identity=_identity(),
                ingress_run_id="crash-before-checkpoint",
                submission=_submission(),
            )
        finally:
            store.close()
        host = await WorkflowGraphHost.open(
            config=config,
            provider_registry={
                "planner": _Planner(),
                "worker": _Worker(),
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )
        try:
            assert await host.recover_nonterminal() == 1
        finally:
            await host.close()
        reopened = await WorkflowGraphHost.open(
            config=config,
            provider_registry={
                "planner": _Planner(),
                "worker": _Worker(),
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )
        try:
            status = await reopened.get_status(
                identity=_identity(), workflow_id=bundle.workflow.workflow_id
            )
            assert status.handle.status == "completed"
        finally:
            await reopened.close()

    asyncio.run(exercise())


def test_recover_nonterminal_reuses_checkpoint_invocation_token(tmp_path) -> None:
    """In-flight recovery must retain child claim ownership from the checkpoint."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost, _token
    from assistant_agent.workflows.graph_state import initial_workflow_graph_state

    async def exercise() -> None:
        config = _config(tmp_path)
        store = SQLiteWorkflowStore(config.durable_workflow_path)
        try:
            bundle = WorkflowService(
                store=store,
                definitions=default_workflow_definitions(),
                submission_engine="langgraph_v3",
            ).submit(
                identity=_identity(),
                ingress_run_id="crash-with-checkpoint",
                submission=_submission(),
            )
        finally:
            store.close()
        host = await WorkflowGraphHost.open(
            config=config,
            provider_registry={
                "planner": _Planner(),
                "worker": _Worker(),
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )
        original_run_id = "workflow-original-invocation"
        execution = host._execution_identity(bundle, run_id=original_run_id)
        initial = initial_workflow_graph_state(
            workflow=bundle.workflow,
            submission=_submission(),
            admitted_plan=None,
            workflow_thread_id=execution.thread_id,
            invocation_run_id=original_run_id,
            invocation_trace_id="workflow-original-trace",
        )
        await host._graph_app.graph.aupdate_state(
            execution.runnable_config(), initial, as_node="__start__"
        )
        observed: list[str] = []
        real_context = host._context

        def observe_context(bundle, *, invocation_token):
            observed.append(invocation_token)
            return real_context(bundle, invocation_token=invocation_token)

        host._context = observe_context
        try:
            assert await host.recover_nonterminal() == 1
        finally:
            await host.close()
        assert observed == [_token(original_run_id)]

    asyncio.run(exercise())


def test_real_api_composition_shares_one_saver_owner(tmp_path, monkeypatch) -> None:
    """Both compiled graphs borrow one process owner; Host must not close it."""

    from assistant_agent.api import app as app_module
    from assistant_agent.api import routes_agent

    config = _config(tmp_path)
    application = FastAPI()
    monkeypatch.setenv(
        "MULTIMODAL_AGENT_WORKFLOW_CUTOVER_MANIFEST_PATH",
        _write_cutover_manifest(tmp_path),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_runtime_config",
        lambda: config,
    )

    def compose_runtime(**kwargs):
        from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp

        runtime = SimpleNamespace(
            assistant_graph_app=AssistantTurnGraphApp(
                checkpointer=kwargs["checkpointer"]
            ),
            workflow_graph_host=kwargs["workflow_graph_host"],
            workflow_service=None,
            workflow_artifact_store=None,
            chat_adapter=_Planner(),
            registry=_registry(),
            close=lambda: True,
        )
        return runtime, None

    monkeypatch.setattr(
        routes_agent,
        "create_agent_runtime_for_composition",
        compose_runtime,
    )

    async def exercise() -> None:
        await app_module.start_shared_checkpointer_owner(application)
        owner = application.state.shared_checkpointer_owner
        try:
            host = await app_module.start_workflow_graph_host(application)
            runtime = app_module.start_shared_agent_runtime(application)
            assert host is not None
            assert host._owner is owner
            assert host._owns_owner is False
            assert (
                runtime.assistant_graph_app.graph.checkpointer.conn
                is owner.checkpointer.conn
                is host._graph_app.graph.checkpointer.conn
            )
            assert runtime.workflow_graph_host is host
            assert runtime.workflow_service is None
            assert runtime.workflow_artifact_store is None
            await app_module.shutdown_workflow_graph_host(application)
            assert (
                owner.checkpointer.conn
                is runtime.assistant_graph_app.graph.checkpointer.conn
            )
            app_module.shutdown_shared_agent_runtime(application)
        finally:
            await app_module.shutdown_shared_checkpointer_owner(application)

    try:
        asyncio.run(exercise())
    finally:
        routes_agent.shutdown_agent_runtime()


def test_api_workflow_host_requires_operator_manifest(tmp_path, monkeypatch) -> None:
    """Production graph admission cannot start without the signed local gate."""

    from assistant_agent.api import app as app_module

    application = FastAPI()
    monkeypatch.delenv(
        "MULTIMODAL_AGENT_WORKFLOW_CUTOVER_MANIFEST_PATH",
        raising=False,
    )
    monkeypatch.setattr(app_module, "resolve_runtime_config", lambda: _config(tmp_path))

    async def exercise() -> None:
        await app_module.start_shared_checkpointer_owner(application)
        try:
            with pytest.raises(RuntimeError, match="operator cutover manifest"):
                await app_module.start_workflow_graph_host(application)
            assert not hasattr(application.state, "workflow_graph_host") or (
                application.state.workflow_graph_host is None
            )
        finally:
            await app_module.shutdown_shared_checkpointer_owner(application)

    asyncio.run(exercise())


def test_workflow_api_get_uses_graph_host_strict_product_snapshot() -> None:
    """HTTP read composition must not route through legacy service or expose native IDs."""

    from assistant_agent.api import routes_workflows
    from assistant_agent.workflows.graph_projection import WorkflowGraphProjector
    from assistant_agent.workflows.graph_state import initial_workflow_graph_state

    record_store = SQLiteWorkflowStore(":memory:")
    service = WorkflowService(
        store=record_store,
        definitions=default_workflow_definitions(),
        submission_engine="langgraph_v3",
    )
    try:
        bundle = service.submit(
            identity=_identity(),
            ingress_run_id="api-graph-read",
            submission=_submission(),
        )
        snapshot = WorkflowGraphProjector().project_snapshot(
            initial_workflow_graph_state(
                workflow=bundle.workflow,
                submission=_submission(),
                admitted_plan=None,
                workflow_thread_id="private-native-thread",
                invocation_run_id="private-native-run",
                invocation_trace_id="private-native-trace",
            )
        )

        class Host:
            async def get_status(self, **_kwargs):
                return snapshot

        response = asyncio.run(
            routes_workflows.get_workflow(
                workflow_id=bundle.workflow.workflow_id,
                identity=_identity(),
                host=Host(),
            )
        )
        body = response.model_dump(mode="json")
        assert body["workflow"]["workflow_id"] == bundle.workflow.workflow_id
        assert body["workflow"]["execution_engine"] == "langgraph_v3"
        assert "plan" not in body
        assert not (
            _forbidden_keys(body)
            & {"checkpoint_id", "thread_id", "invocation_run_id", "task_id"}
        )
    finally:
        record_store.close()


def test_workflow_api_resume_and_cancel_are_graph_host_owned() -> None:
    """API actions use stable product refs, never legacy resume/native IDs."""

    from assistant_agent.api import routes_workflows
    from assistant_agent.workflows.graph_host import WorkflowGraphHandle

    calls: list[tuple[str, dict[str, object]]] = []

    class Host:
        async def resume(self, **kwargs):
            calls.append(("resume", kwargs))
            return WorkflowGraphHandle(
                workflow_id="workflow-api-action",
                workflow_type="deep_research",
                execution_engine="langgraph_v3",
                status="running",
                phase="executing",
                output_ref="workflow://workflow-api-action",
            )

        async def cancel(self, **kwargs):
            calls.append(("cancel", kwargs))
            return WorkflowGraphHandle(
                workflow_id="workflow-api-action",
                workflow_type="deep_research",
                execution_engine="langgraph_v3",
                status="cancelled",
                phase="cancelled",
                output_ref="workflow://workflow-api-action",
            )

    resume_body = routes_workflows.WorkflowInputRequest.model_validate(
        {
            "action_ref": "workflow:workflow-api-action:node:collect:generation:0",
            "values": {"answer": "approved"},
        }
    )
    assert "resume_token" not in resume_body.model_dump(mode="json")
    asyncio.run(
        routes_workflows.provide_workflow_input(
            workflow_id="workflow-api-action",
            body=resume_body,
            identity=_identity(),
            host=Host(),
        )
    )
    asyncio.run(
        routes_workflows.cancel_workflow(
            workflow_id="workflow-api-action",
            body=routes_workflows.WorkflowCancelRequest(),
            identity=_identity(),
            host=Host(),
        )
    )
    assert [name for name, _payload in calls] == ["resume", "cancel"]
    assert calls[0][1]["action_ref"] == (
        "workflow:workflow-api-action:node:collect:generation:0"
    )


def test_legacy_waiting_row_keeps_product_action_facade_during_drain(tmp_path) -> None:
    """A frozen legacy row must resume without exposing its raw resume token."""

    from assistant_agent.api import routes_workflows
    from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
    from assistant_agent.workflows.legacy_drain_host import LegacyDrainHost

    store = SQLiteWorkflowStore(tmp_path / "legacy-products.sqlite3")
    artifact_store = LocalWorkflowArtifactStore(tmp_path / "legacy-artifacts")
    service = WorkflowService(
        store=store,
        definitions=default_workflow_definitions(),
    )
    bundle = service.submit(
        identity=_identity(),
        ingress_run_id="legacy-waiting-api",
        submission=_submission(),
    )
    changed = bundle.model_copy(deep=True)
    item = changed.current_plan.work_items[0]
    item.status = "blocked"
    changed.workflow.status = "waiting_input"
    changed.workflow.phase = "waiting_input"
    changed.workflow.waiting_input = {
        "required_fields": ["answer"],
        "prompt_code": "answer_required",
        "safe_prompt": "Please provide the requested answer.",
        "resume_token": "private-legacy-resume-token",
    }
    saved = store.save(
        changed,
        expected_revision=bundle.workflow.revision,
        events=[],
    )
    drain = LegacyDrainHost(
        service=service,
        artifact_store=artifact_store,
        worker=None,
        allowed_workflow_ids=frozenset({saved.workflow.workflow_id}),
    )

    class GraphHost:
        async def get_status(self, **_kwargs):
            raise AssertionError("legacy row reached graph status")

        async def resume(self, **_kwargs):
            raise AssertionError("legacy row reached graph resume")

        async def get_events(self, **_kwargs):
            raise AssertionError("legacy row reached graph events")

        async def get_result(self, **_kwargs):
            raise AssertionError("legacy row reached graph result")

        async def cancel(self, **_kwargs):
            raise AssertionError("legacy row reached graph cancel")

    try:
        response = asyncio.run(
            routes_workflows.get_workflow(
                workflow_id=saved.workflow.workflow_id,
                identity=_identity(),
                host=GraphHost(),
                legacy_host=drain,
            )
        )
        body = response.model_dump(mode="json")
        assert body["workflow"]["execution_engine"] == "legacy_scheduler_v2"
        assert "resume_token" not in str(body)
        assert f":node:{item.work_item_id}:" not in body["waiting_actions"][0][
            "action_ref"
        ]
        assert all(
            active["node_id"] != item.work_item_id
            for active in body["progress"]["active_items"]
        )
        assert body["waiting_actions"][0]["node_id"].startswith("legacy_")
        action_ref = body["waiting_actions"][0]["action_ref"]
        action = asyncio.run(
            routes_workflows.provide_workflow_input(
                workflow_id=saved.workflow.workflow_id,
                body=routes_workflows.WorkflowInputRequest(
                    action_ref=action_ref,
                    values={"answer": "approved"},
                ),
                identity=_identity(),
                host=GraphHost(),
                legacy_host=drain,
            )
        )
        assert action.workflow.execution_engine == "legacy_scheduler_v2"
        resumed = store.load(saved.workflow.workflow_id)
        assert resumed is not None
        assert resumed.workflow.status == "queued"
        assert resumed.workflow.consumed_resume_tokens == [
            "private-legacy-resume-token"
        ]
        events = asyncio.run(
            routes_workflows.get_workflow_events(
                workflow_id=saved.workflow.workflow_id,
                after=0,
                limit=100,
                identity=_identity(),
                host=GraphHost(),
                legacy_host=drain,
            )
        )
        assert events.events
        assert "resume_token" not in str(events.model_dump(mode="json"))
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                routes_workflows.get_workflow_result(
                    workflow_id=saved.workflow.workflow_id,
                    identity=_identity(),
                    host=GraphHost(),
                    legacy_host=drain,
                )
            )
        assert getattr(exc_info.value, "status_code", None) == 409
        cancelled = asyncio.run(
            routes_workflows.cancel_workflow(
                workflow_id=saved.workflow.workflow_id,
                body=routes_workflows.WorkflowCancelRequest(),
                identity=_identity(),
                host=GraphHost(),
                legacy_host=drain,
            )
        )
        assert cancelled.workflow.execution_engine == "legacy_scheduler_v2"
    finally:
        asyncio.run(drain.close())


def test_legacy_archived_long_horizon_remains_queryable(tmp_path) -> None:
    """Archived legacy types use their faithful DTO instead of graph literals."""

    from assistant_agent.api import routes_workflows
    from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
    from assistant_agent.workflows.legacy_drain_host import LegacyDrainHost

    store = SQLiteWorkflowStore(tmp_path / "archive-products.sqlite3")
    artifact_store = LocalWorkflowArtifactStore(tmp_path / "archive-artifacts")
    service = WorkflowService(store=store, definitions=default_workflow_definitions())
    submission = _submission().model_copy(
        update={
            "workflow_type": "long_horizon",
            "idempotency_key": "legacy-archive-long-horizon",
        }
    )
    bundle = service.submit(
        identity=_identity(),
        ingress_run_id="legacy-archive-read",
        submission=submission,
    )
    changed = bundle.model_copy(deep=True)
    changed.workflow.status = "completed"
    changed.workflow.phase = "completed"
    changed.workflow.terminal_at = datetime.now(timezone.utc)
    archived = store.save(
        changed,
        expected_revision=bundle.workflow.revision,
        events=[],
    )
    drain = LegacyDrainHost(
        service=service,
        artifact_store=artifact_store,
        worker=None,
        allowed_workflow_ids=frozenset(),
    )

    class GraphHost:
        async def get_status(self, **_kwargs):
            raise AssertionError("legacy archive reached graph status")

        async def get_events(self, **_kwargs):
            raise AssertionError("legacy archive reached graph events")

        async def get_result(self, **_kwargs):
            raise AssertionError("legacy archive reached graph result")

    try:
        response = asyncio.run(
            routes_workflows.get_workflow(
                workflow_id=archived.workflow.workflow_id,
                identity=_identity(),
                host=GraphHost(),
                legacy_host=drain,
            )
        )
        assert response.workflow.workflow_type == "long_horizon"
        events = asyncio.run(
            routes_workflows.get_workflow_events(
                workflow_id=archived.workflow.workflow_id,
                after=0,
                limit=100,
                identity=_identity(),
                host=GraphHost(),
                legacy_host=drain,
            )
        )
        assert events.events
        with pytest.raises(Exception) as exc_info:
            asyncio.run(
                routes_workflows.get_workflow_result(
                    workflow_id=archived.workflow.workflow_id,
                    identity=_identity(),
                    host=GraphHost(),
                    legacy_host=drain,
                )
            )
        assert getattr(exc_info.value, "status_code", None) == 404
    finally:
        asyncio.run(drain.close())


def test_workflow_cancel_reason_is_validated_before_persistence() -> None:
    """Public cancel only accepts the stable product reason allowlist."""

    from assistant_agent.api import routes_workflows

    with pytest.raises(ValueError):
        routes_workflows.WorkflowCancelRequest(reason_code="invalid reason / private")


def test_real_host_resume_and_cancel_use_only_product_action_refs(tmp_path) -> None:
    """Native interrupt IDs remain private across restart, resume, and cancel."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def open_host(worker):
        return await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry={
                "planner": _Planner(),
                "worker": worker,
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )

    async def wait_for_action(host, workflow_id):
        for _attempt in range(100):
            snapshot = await host.get_status(
                identity=_identity(), workflow_id=workflow_id
            )
            if snapshot.waiting_actions:
                return snapshot
            await asyncio.sleep(0.01)
        raise AssertionError("workflow did not expose a product action")

    async def exercise() -> None:
        worker = _BlockingWorker()
        first = await open_host(worker)
        handle = await first.start(
            identity=_identity(),
            ingress_run_id="resume-product-action",
            submission=_submission(),
        )
        waiting = await wait_for_action(first, handle.workflow_id)
        action = waiting.waiting_actions[0]
        assert action.action_ref.startswith(f"workflow:{handle.workflow_id}:node:")
        assert "interrupt" not in action.action_ref
        await first.resume(
            identity=_identity(),
            workflow_id=handle.workflow_id,
            action_ref=action.action_ref,
            values={"answer": "approved"},
        )
        await first.close()

        second = await open_host(_BlockingWorker())
        try:
            completed = await second.get_status(
                identity=_identity(), workflow_id=handle.workflow_id
            )
            assert completed.handle.status == "completed"
            cancel_handle = await second.start(
                identity=_identity(),
                ingress_run_id="cancel-product-action",
                submission=_submission().model_copy(
                    update={"idempotency_key": "host-cancel-1"}
                ),
            )
            await wait_for_action(second, cancel_handle.workflow_id)
            cancelled = await second.cancel(
                identity=_identity(),
                workflow_id=cancel_handle.workflow_id,
            )
            assert cancelled.status == "cancelled"
            assert (
                await second.get_status(
                    identity=_identity(), workflow_id=cancel_handle.workflow_id
                )
            ).handle.status == "cancelled"
        finally:
            await second.close()

    asyncio.run(exercise())
