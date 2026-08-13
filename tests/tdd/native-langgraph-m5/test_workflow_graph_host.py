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
from assistant_agent.workflows.store import InMemoryWorkflowStore
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
            assert not (_forbidden_keys(events.model_dump(mode="json")) & forbidden)

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
        app_module, "shutdown_workflow_graph_host", async_call("stop_graph_host")
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
    monkeypatch.setattr(
        app_module, "prepare_server_startup_report", lambda *_args: None
    )

    async def exercise() -> None:
        async with app_module._lifespan(application):
            assert calls == [
                "start_saver_owner",
                "start_graph_host",
                "start_runtime",
                "start_tasks",
                "recover_nonterminal",
            ]

    asyncio.run(exercise())
    assert calls == [
        "start_saver_owner",
        "start_graph_host",
        "start_runtime",
        "start_tasks",
        "recover_nonterminal",
        "stop_graph_host",
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


def test_graph_host_direct_invocation_uses_product_submission_and_persists_projection(
    tmp_path,
) -> None:
    """Eval targets must run the production host graph, not a second workflow engine."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        host = await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry={
                "planner": _Planner(),
                "worker": _Worker(),
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )
        try:
            result = await host.arun_submission(
                identity=_identity(),
                ingress_run_id="langsmith-direct-invocation",
                submission=_submission(),
            )
            snapshot = await host.get_status(
                identity=_identity(),
                workflow_id=result.final_state["workflow_id"],
            )
            assert result.status == "completed"
            assert result.final_state["execution_engine"] == "langgraph_v3"
            assert snapshot.handle.status == "completed"
        finally:
            await host.close()

    asyncio.run(exercise())


def test_graph_host_direct_invocation_resumes_native_interrupt_with_new_run_id(
    tmp_path,
) -> None:
    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        host = await WorkflowGraphHost.open(
            config=_config(tmp_path),
            provider_registry={
                "planner": _Planner(),
                "worker": _BlockingWorker(),
                "verifier": _Verifier(),
            },
            tool_registry=_registry(),
        )
        try:
            seen = []

            def resume_values(interrupts):
                seen.extend(interrupts)
                return {
                    item.action_ref: {
                        field: "operator-sentinel" for field in item.required_fields
                    }
                    for item in interrupts
                }

            result = await host.arun_submission(
                identity=_identity(),
                ingress_run_id="langsmith-native-resume",
                submission=_submission(),
                resume_values_factory=resume_values,
            )
            assert result.status == "completed"
            assert seen and all(item.action_ref for item in seen)
            assert tuple(result.final_state["consumed_action_refs"]) == tuple(
                sorted(item.action_ref for item in seen)
            )
            assert len(result.final_state["invocation_run_ids"]) == 2
            assert (
                result.final_state["invocation_run_ids"][0]
                != (result.final_state["invocation_run_ids"][1])
            )
        finally:
            await host.close()

    asyncio.run(exercise())


def test_recover_nonterminal_starts_graph_admission_without_checkpoint(
    tmp_path,
) -> None:
    """Startup recovery must execute an admitted graph row after a pre-checkpoint crash."""

    from assistant_agent.workflows.graph_host import WorkflowGraphHost

    async def exercise() -> None:
        config = _config(tmp_path)
        store = SQLiteWorkflowStore(config.durable_workflow_path)
        try:
            bundle = WorkflowService(
                store=store,
                definitions=default_workflow_definitions(),
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


def test_workflow_api_get_uses_graph_host_strict_product_snapshot() -> None:
    """HTTP read composition must not route through legacy service or expose native IDs."""

    from assistant_agent.api import routes_workflows
    from assistant_agent.workflows.graph_projection import WorkflowGraphProjector
    from assistant_agent.workflows.graph_state import initial_workflow_graph_state

    record_store = SQLiteWorkflowStore(":memory:")
    service = WorkflowService(
        store=record_store,
        definitions=default_workflow_definitions(),
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


def test_nonterminal_legacy_row_is_not_executable_after_retirement() -> None:
    """A retired legacy row must never regain resume or cancel authority."""

    from assistant_agent.workflows.graph_host import (
        WorkflowGraphHostError,
        _archived_snapshot,
    )

    store = InMemoryWorkflowStore()
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
    changed.workflow.execution_engine = "legacy_scheduler_v2"
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
    with pytest.raises(WorkflowGraphHostError) as exc_info:
        _archived_snapshot(saved)
    assert exc_info.value.code == "workflow_legacy_nonterminal_retired"
    unchanged = store.load(saved.workflow.workflow_id)
    assert unchanged is not None
    assert unchanged.workflow.status == "waiting_input"
    store.close()


def test_legacy_archived_long_horizon_remains_queryable() -> None:
    """Archived legacy types use their faithful DTO instead of graph literals."""

    from assistant_agent.api import routes_workflows
    from assistant_agent.workflows.graph_host import _archived_snapshot

    store = InMemoryWorkflowStore()
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
    changed.workflow.execution_engine = "legacy_scheduler_v2"
    changed.workflow.status = "completed"
    changed.workflow.phase = "completed"
    changed.workflow.terminal_at = datetime.now(timezone.utc)
    archived = store.save(
        changed,
        expected_revision=bundle.workflow.revision,
        events=[],
    )
    snapshot = _archived_snapshot(archived)
    response = routes_workflows._graph_workflow_response(snapshot)
    assert response.workflow.workflow_type == "long_horizon"
    assert response.workflow.execution_engine == "legacy_scheduler_v2"
    assert response.workflow.status == "completed"
    assert response.waiting_actions == ()
    store.close()


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
