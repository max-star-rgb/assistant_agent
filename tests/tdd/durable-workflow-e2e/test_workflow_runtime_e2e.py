from __future__ import annotations

import json

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.tools.ids import WORKFLOW_SUBMIT_TOOL_NAME
from assistant_agent.tools.plugins.builtin.workflow.tool import WorkflowSubmitTool
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import InMemoryWorkflowStore
from assistant_agent.workflows.worker import DurableWorkflowWorker


class WorkflowScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self.results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self.results)


class WorkflowMemoryProbe:
    def __init__(self) -> None:
        self.attached_workflow_runs = 0
        self.enqueued_workflow_runs = 0

    def prepare_context(self, *, state, trace_store, cancel_token) -> SessionMemorySnapshot:
        snapshot = SessionMemorySnapshot()
        state.session_memory_snapshot = snapshot
        return snapshot

    def release_run_context(self, *, identity, run_id) -> bool:
        return False

    def attach_session_snapshot(self, state) -> None:
        if "_trusted_workflow_assignment" in state.request.metadata:
            self.attached_workflow_runs += 1

    def enqueue_completed_turn(self, *, trace_store, state) -> bool:
        if "_trusted_workflow_assignment" in state.request.metadata:
            self.enqueued_workflow_runs += 1
        return False

    def close(self, *, timeout=None) -> bool:
        return True


def test_provider_native_react_autonomously_submits_workflow_without_classifier(tmp_path) -> None:
    service = WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=default_workflow_definitions(),
    )
    registry = ToolRegistry()
    registry.register(WorkflowSubmitTool(service))
    registry.seal()
    adapter = WorkflowScriptedChatAdapter([
        ChatResult(
            provider="scripted",
            model="scripted-model",
            finish_reason="tool_calls",
            tool_calls=[NativeToolCall(
                id="workflow-call-sentinel",
                name=WORKFLOW_SUBMIT_TOOL_NAME,
                arguments={
                    "workflow_type": "long_horizon",
                    "objective": "objective-sentinel",
                    "deliverables": ["deliverable-sentinel"],
                    "constraints": [],
                    "inputs": {},
                    "requested_budget": {},
                    "durability_reasons": ["multi_stage"],
                    "idempotency_key": "submission-sentinel",
                },
                )],
            ),
        ChatResult(
            provider="scripted",
            model="scripted-model",
            finish_reason="stop",
            response_text=json.dumps(
                {
                    "workflow_plan": {
                        "workstreams": [
                            {
                                "seed_id": f"work-{index}",
                                "kind": "execute",
                                "display_title": f"正在执行阶段 {index}",
                                "objective": f"work-item-{index}-objective",
                                "depends_on": ([f"work-{index - 1}"] if index > 1 else []),
                                "acceptance_contract": {},
                            }
                            for index in range(1, 4)
                        ],
                        "constraint_bindings": [],
                    }
                },
                ensure_ascii=False,
            ),
        ),
        *[
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text=f"work-item-{index}-sentinel",
            )
            for index in range(1, 4)
        ],
    ])
    runtime = AgentGraphRuntime(
        registry=registry,
        workflow_service=service,
        config=ProviderConfig(
            durable_workflows_enabled=True,
            durable_workflow_artifact_path=str(tmp_path / "artifacts"),
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        long_term_memory_service=(memory_probe := WorkflowMemoryProbe()),
    )

    state = runtime.run_state(UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="完成一个需要多个长期阶段的任务。",
    ))

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == [WORKFLOW_SUBMIT_TOOL_NAME]
    assert state.tool_results[0].success is True
    assert state.response.data["handoff"]["kind"] == "durable_workflow"
    assert len(adapter.requests) == 1
    workflow_id = state.tool_results[0].data["workflow"]["workflow_id"]
    assert service.store.load(workflow_id).workflow.status == "queued"
    assert adapter.requests[0].tools[0]["function"]["name"] == WORKFLOW_SUBMIT_TOOL_NAME
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(
            service=service,
            work_item_executor=AgentRuntimeWorkItemExecutor(
                agent_runtime=runtime,
                artifact_store=runtime.workflow_artifact_store,
                context_compiler=WorkflowContextCompiler(
                    artifact_store=runtime.workflow_artifact_store
                ),
            ),
        ),
        worker_id="worker-sentinel",
    )
    for _ in range(4):
        assert worker.run_once() is True
    completed = service.store.load(workflow_id)
    assert completed.workflow.status == "completed"
    assert len(adapter.requests) == 5
    assert all(request.tools == [] for request in adapter.requests[2:])
    assert memory_probe.attached_workflow_runs == 0
    assert memory_probe.enqueued_workflow_runs == 0
    final_ref = completed.workflow.result_artifact_refs[0]
    assert runtime.workflow_artifact_store.read_text(
        identity=RequestIdentity.for_user(
            user_id="user-sentinel",
            agent_id=runtime.agent_id,
            session_id="session-sentinel",
        ),
        artifact_ref=final_ref,
    ) == "work-item-3-sentinel"
    runtime.workflow_artifact_store.close()
    runtime.close()
