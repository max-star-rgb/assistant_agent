from threading import Event
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.tools import ApprovalPolicy, ToolPolicyMetadata, ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.services.durable_tasks.worker import (
    DurableTaskWorker,
    _binding_for_lease,
    _resume_request,
)
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class QueryInput(BaseModel):
    query: str | None = None
    prompt: str | None = None


class RecordingTool(MockTool):
    description = "record a query"
    input_schema = QueryInput
    output_schema = QueryInput

    def __init__(self, name: str = "product_search") -> None:
        self.name = name
        self.calls = 0

    def _run(self, input: QueryInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        text = input.query or input.prompt or ""
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"summary": f"result for {text}"},
            output_ref=f"mock://{self.name}/{self.calls}",
        )


class ScriptedAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        result = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return result


class UncertainWriteTool(RecordingTool):
    policy = ToolPolicyMetadata(
        risk="external_write",
        approval=ApprovalPolicy(mode="never"),
    )

    def _run(self, input: QueryInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(
            tool_name=self.name,
            success=False,
            error="provider_timeout: commit status unavailable",
        )


def test_worker_returns_false_when_no_task_is_claimable() -> None:
    worker, _, _, _ = _worker([_final("unused")])

    assert worker.run_once() is False


def test_worker_executes_one_ready_step_and_checkpoints_before_release() -> None:
    worker, service, tool, _ = _worker([_native("product_search", {"query": "耳机"})])
    bundle = _submit(service, tool_name="product_search")

    assert worker.run_once() is True

    stored = service.store.load(bundle.task.task_id)
    run = stored.step_runs[0]
    assert tool.calls == 1
    assert run.status == "succeeded"
    assert run.output_ref == "mock://product_search/1"
    assert stored.task.lease_token is None
    assert [event.event_type for event in service.store.list_events(bundle.task.task_id)][-1] == "step.completed"


def test_worker_requires_confirmation_then_resumes_with_bound_approval() -> None:
    worker, service, tool, adapter = _worker(
        [
            _native("custom_notification", {"query": "通知团队"}),
            _native("custom_notification", {"query": "通知团队"}),
        ],
        tool_name="custom_notification",
    )
    bundle = _submit(service, tool_name="custom_notification")

    assert worker.run_once() is True
    waiting = service.store.load(bundle.task.task_id)
    assert waiting.task.status == "waiting_confirmation"
    assert tool.calls == 0
    confirmation = waiting.confirmations[-1]

    service.confirm(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        task_id=bundle.task.task_id,
        confirmation_id=confirmation.confirmation_id,
        approved=True,
    )
    assert worker.run_once() is True

    resumed = service.store.load(bundle.task.task_id)
    assert adapter.calls == 2
    assert tool.calls == 1
    assert resumed.step_runs[0].status == "succeeded"


def test_worker_rejects_changed_input_after_confirmation() -> None:
    worker, service, tool, _ = _worker(
        [
            _native("custom_notification", {"query": "通知团队"}),
            _native("custom_notification", {"query": "改为删除记录"}),
        ],
        tool_name="custom_notification",
    )
    bundle = _submit(service, tool_name="custom_notification")
    worker.run_once()
    waiting = service.store.load(bundle.task.task_id)
    service.confirm(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        task_id=bundle.task.task_id,
        confirmation_id=waiting.confirmations[-1].confirmation_id,
        approved=True,
    )

    worker.run_once()

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 0
    assert stored.task.status == "replanning"
    assert stored.step_runs[0].error_code == "durable_confirmation_binding_mismatch"


def test_worker_rejects_premature_natural_language_completion() -> None:
    worker, service, tool, _ = _worker([_final("已经完成")])
    bundle = _submit(service, tool_name="product_search")

    assert worker.run_once() is True

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 0
    assert stored.task.status == "replanning"
    assert stored.step_runs[0].error_code == "durable_step_required"


def test_worker_can_revise_the_bound_plan_without_leaving_a_stale_lease() -> None:
    revised_plan = TaskPlan(
        goal="修订后的任务",
        steps=[TaskStep(step_id="step_1", action="重新搜索", tool_name="product_search")],
    )
    worker, service, _, _ = _worker(
        [
            _native(
                "task_plan_submit",
                {
                    "plan": revised_plan.model_dump(mode="json"),
                    "revision_reason": "new evidence",
                },
            )
        ]
    )
    bundle = _submit(service, tool_name="product_search")

    worker.run_once()

    stored = service.store.load(bundle.task.task_id)
    assert stored.task.current_plan_version == 2
    assert stored.task.objective == "修订后的任务"
    assert stored.task.lease_token is None


def test_waiting_input_is_not_claimed_until_identity_bound_input_arrives() -> None:
    worker, service, tool, adapter = _worker(
        [_native("product_search", {"query": "用户补充的预算"})]
    )
    bundle = service.submit_plan(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        ingress_run_id="run-ingress",
        plan=TaskPlan(
            goal="等待预算后搜索",
            steps=[TaskStep(step_id="step_1", action="搜索", tool_name="product_search")],
            requires_followup=True,
            followup_question="预算是多少？",
        ),
        revision_reason="initial",
    )

    assert worker.run_once() is False
    service.provide_input(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        task_id=bundle.task.task_id,
        text="预算 500 元",
    )
    assert worker.run_once() is True

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 1
    assert "User-provided task input: 预算 500 元" in stored.task.active_constraints
    assert "预算 500 元" in str(adapter.requests[0].messages)


def test_invalid_ready_step_input_checkpoints_waiting_input() -> None:
    worker, service, tool, _ = _worker([_native("product_search", {})])
    bundle = _submit(service, tool_name="product_search")

    worker.run_once()

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 0
    assert stored.task.status == "waiting_input"
    assert stored.step_runs[0].status == "waiting_input"
    assert stored.task.lease_token is None


def test_worker_uses_a_separate_completion_quantum_after_required_steps() -> None:
    worker, service, _, adapter = _worker(
        [
            _native("product_search", {"query": "耳机"}),
            _final("任务完成"),
        ]
    )
    bundle = _submit(service, tool_name="product_search")

    assert worker.run_once() is True
    assert service.store.load(bundle.task.task_id).task.status == "running"
    assert worker.run_once() is True

    stored = service.store.load(bundle.task.task_id)
    assert adapter.calls == 2
    assert stored.task.status == "completed"
    assert stored.task.terminal_at is not None


def test_expired_lease_retries_read_only_step_after_pre_checkpoint_crash() -> None:
    worker, service, tool, _ = _worker(
        [
            _native("product_search", {"query": "耳机"}),
            _native("product_search", {"query": "耳机"}),
        ]
    )
    bundle = _submit(service, tool_name="product_search")
    now = datetime.now(timezone.utc)
    _run_without_checkpoint(worker, service, now)

    assert worker.run_once(now=now + timedelta(seconds=31)) is True

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 2
    assert stored.step_runs[0].status == "succeeded"


def test_expired_lease_suppresses_duplicate_compensatable_side_effect() -> None:
    worker, service, tool, _ = _worker(
        [
            _native("image_generation", {"prompt": "耳机海报"}),
            _native("image_generation", {"prompt": "耳机海报"}),
        ],
        tool_name="image_generation",
    )
    bundle = _submit(service, tool_name="image_generation")
    now = datetime.now(timezone.utc)
    _run_without_checkpoint(worker, service, now)

    assert worker.run_once(now=now + timedelta(seconds=31)) is True

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 1
    assert stored.step_runs[0].status == "succeeded"


def test_mutating_timeout_checkpoints_outcome_unknown_without_retry() -> None:
    registry = ToolRegistry()
    tool = UncertainWriteTool("external_write_probe")
    registry.register(tool)
    service = DurableTaskService(store=InMemoryTaskStore(), registry=registry)
    adapter = ScriptedAdapter(
        [_native("external_write_probe", {"query": "提交变更"})]
    )
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(durable_tasks_enabled=True),
        chat_adapter=adapter,
        durable_task_service=service,
    )
    worker = DurableTaskWorker(
        service=service,
        runtime=runtime,
        worker_id="worker-test",
        poll_seconds=0.01,
    )
    bundle = _submit(service, tool_name="external_write_probe")

    worker.run_once()

    stored = service.store.load(bundle.task.task_id)
    assert tool.calls == 1
    assert stored.task.status == "outcome_unknown"
    assert worker.run_once() is False


def test_cancelled_quantum_checkpoints_task_before_any_tool_call() -> None:
    worker, service, tool, adapter = _worker(
        [_native("product_search", {"query": "耳机"})]
    )
    bundle = _submit(service, tool_name="product_search")
    lease = service.claim_next(worker_id="cancel-worker")
    snapshot = service.snapshot_for_lease(lease)
    stored = service.store.load(lease.task_id)
    binding = _binding_for_lease(lease, stored, snapshot.ready_step_ids)
    request = _resume_request(stored.task.user_id, stored.task.session_id, snapshot, binding)
    cancel = Event()
    cancel.set()

    result = worker.runtime.run_task_quantum(
        request,
        binding=binding,
        cancel_token=cancel,
    )
    service.checkpoint(lease, result.checkpoint)

    cancelled = service.store.load(bundle.task.task_id)
    assert result.checkpoint.kind == "cancelled"
    assert adapter.calls == 0
    assert tool.calls == 0
    assert cancelled.task.status == "cancelled"


def test_worker_loop_uses_cooperative_stop_event() -> None:
    worker, _, _, _ = _worker([_final("unused")])
    stop = Event()
    stop.set()

    worker.run(stop)


def _worker(outputs: list[ChatResult], *, tool_name: str = "product_search"):
    registry = ToolRegistry()
    tool = RecordingTool(tool_name)
    registry.register(tool)
    service = DurableTaskService(store=InMemoryTaskStore(), registry=registry)
    adapter = ScriptedAdapter(outputs)
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(durable_tasks_enabled=True),
        chat_adapter=adapter,
        durable_task_service=service,
    )
    worker = DurableTaskWorker(
        service=service,
        runtime=runtime,
        worker_id="worker-test",
        poll_seconds=0.01,
    )
    return worker, service, tool, adapter


def _submit(service: DurableTaskService, *, tool_name: str):
    return service.submit_plan(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        ingress_run_id="run-ingress",
        plan=TaskPlan(
            goal="完成任务",
            steps=[TaskStep(step_id="step_1", action="执行", tool_name=tool_name)],
        ),
        revision_reason="initial",
    )


def _run_without_checkpoint(
    worker: DurableTaskWorker,
    service: DurableTaskService,
    now: datetime,
) -> None:
    lease = service.claim_next(worker_id="crashed-worker", now=now)
    snapshot = service.snapshot_for_lease(lease)
    bundle = service.store.load(lease.task_id)
    binding = _binding_for_lease(lease, bundle, snapshot.ready_step_ids)
    request = _resume_request(bundle.task.user_id, bundle.task.session_id, snapshot, binding)
    result = worker.runtime.run_task_quantum(request, binding=binding)
    assert result.checkpoint.kind == "tool_succeeded"


def _native(name: str, arguments: dict) -> ChatResult:
    return ChatResult(
        response_text="",
        tool_calls=[NativeToolCall(id="call_1", name=name, arguments=arguments)],
        finish_reason="tool_calls",
        message_kind="tool_call",
        provider="scripted-native",
        model="worker-test",
    )


def _final(message: str) -> ChatResult:
    return ChatResult(
        response_text=message,
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-native",
        model="worker-test",
    )
