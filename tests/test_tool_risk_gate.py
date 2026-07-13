from pydantic import BaseModel

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
    ToolSideEffectPolicy,
)
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.realtime_task_state import (
    RealtimeTaskState,
    reduce_realtime_task_state_event,
)
from assistant_agent.services.tool_risk_gate import (
    InMemoryToolIdempotencyLedger,
    risk_gate_level_for_policy,
)
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class RiskGateInput(BaseModel):
    prompt: str | None = None
    query: str | None = None
    text: str | None = None
    idempotency_key: str | None = None


class RecordingTool(MockTool):
    description = "Records how often it executes."
    input_schema = RiskGateInput
    output_schema = RiskGateInput

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def _run(self, input: RiskGateInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"summary": f"{self.name} execution {self.calls}"},
            output_ref=f"mock://{self.name}/{self.calls}",
        )


class MutableCancelToken:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return {
            "cancel_source": "interrupt",
            "cancel_reason": "barge_in_after_side_effect",
        }


def _realtime_state(*, run_id: str = "run-1") -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="实时任务",
            metadata={"source": "realtime_agent_backend"},
        ),
        run_id=run_id,
    )


def test_risk_gate_maps_side_effect_policy_to_runtime_gate_level() -> None:
    assert risk_gate_level_for_policy(
        ToolSideEffectPolicy(level="external_read", requires_confirmation=False)
    ) == "auto"
    assert risk_gate_level_for_policy(
        ToolSideEffectPolicy(level="local_read", requires_confirmation=False)
    ) == "auto"
    assert risk_gate_level_for_policy(
        ToolSideEffectPolicy(level="compensatable", requires_confirmation=False)
    ) == "soft_gate"
    assert risk_gate_level_for_policy(ToolSideEffectPolicy()) == "hard_gate"


def test_read_only_tools_run_without_idempotency_overhead() -> None:
    tool = RecordingTool("product_search")
    registry = ToolRegistry()
    registry.register(tool)
    ledger = InMemoryToolIdempotencyLedger()
    sink = ListEventSink()
    executor = ToolExecutor(registry=registry, event_sink=sink, idempotency_ledger=ledger)

    first = executor.run_tool(_realtime_state(run_id="run-1"), "step-1", tool.name, {"query": "耳机"})
    second = executor.run_tool(_realtime_state(run_id="run-2"), "step-1", tool.name, {"query": "耳机"})

    assert first.success is True
    assert second.success is True
    assert tool.calls == 2
    assert ledger.record_count == 0
    started = [event for event in sink.events if event.type == "tool_started"]
    assert started[0].payload["pre_tool_call"]["risk_gate"]["level"] == "auto"
    assert started[0].payload["pre_tool_call"]["idempotency"]["required"] is False


def test_compensatable_tool_duplicate_idempotency_key_suppresses_second_execution() -> None:
    tool = RecordingTool("image_generation")
    registry = ToolRegistry()
    registry.register(tool)
    ledger = InMemoryToolIdempotencyLedger()

    first = ToolExecutor(registry=registry, idempotency_ledger=ledger).run_tool(
        _realtime_state(run_id="run-1"),
        "step-1",
        tool.name,
        {"prompt": "蓝牙耳机海报", "idempotency_key": "image-task-1"},
    )
    sink = ListEventSink()
    second = ToolExecutor(registry=registry, event_sink=sink, idempotency_ledger=ledger).run_tool(
        _realtime_state(run_id="run-2"),
        "step-1",
        tool.name,
        {"prompt": "蓝牙耳机海报", "idempotency_key": "image-task-1"},
    )

    assert first.success is True
    assert second.success is True
    assert tool.calls == 1
    assert second.output_ref == first.output_ref
    assert second.data["idempotency"]["duplicate_suppressed"] is True
    finished = next(event for event in sink.events if event.type == "tool_finished")
    post = finished.payload["post_tool_call"]
    assert post["status"] == "duplicate_suppressed"
    assert post["idempotency"]["duplicate_suppressed"] is True


def test_compensatable_tool_generates_default_idempotency_key_for_same_run_step() -> None:
    tool = RecordingTool("image_generation")
    registry = ToolRegistry()
    registry.register(tool)
    ledger = InMemoryToolIdempotencyLedger()
    sink = ListEventSink()
    executor = ToolExecutor(registry=registry, event_sink=sink, idempotency_ledger=ledger)
    state = _realtime_state(run_id="run-1")

    first = executor.run_tool(state, "step-1", tool.name, {"prompt": "蓝牙耳机海报"})
    second = executor.run_tool(state, "step-1", tool.name, {"prompt": "蓝牙耳机海报"})

    assert first.success is True
    assert second.success is True
    assert tool.calls == 1
    assert second.data["idempotency"]["duplicate_suppressed"] is True
    started = [event for event in sink.events if event.type == "tool_started"]
    assert started[0].payload["pre_tool_call"]["idempotency"]["generated"] is True
    assert started[0].payload["pre_tool_call"]["idempotency"]["key"].startswith("auto:")


def test_compensatable_tool_success_after_interrupt_is_committed_not_cancelled() -> None:
    token = MutableCancelToken()

    class InterruptAfterCommitTool(RecordingTool):
        def _run(self, input: RiskGateInput, context: ToolContext) -> ToolResult:
            result = super()._run(input, context)
            token.cancelled = True
            return result

    tool = InterruptAfterCommitTool("image_generation")
    registry = ToolRegistry()
    registry.register(tool)
    ledger = InMemoryToolIdempotencyLedger()
    sink = ListEventSink()

    result = ToolExecutor(
        registry=registry,
        event_sink=sink,
        idempotency_ledger=ledger,
        cancel_token=token,
    ).run_tool(
        _realtime_state(run_id="run-1"),
        "step-1",
        tool.name,
        {"prompt": "蓝牙耳机海报", "idempotency_key": "image-task-1"},
    )

    assert result.success is True
    assert tool.calls == 1
    assert ledger.record_count == 1
    assert not [event for event in sink.events if event.type == "tool_failed"]
    finished = next(event for event in sink.events if event.type == "tool_finished")
    post = finished.payload["post_tool_call"]
    assert post["status"] == "succeeded"
    assert post["idempotency"]["status"] == "committed"
    assert "cancel" not in post


def test_tool_owned_hard_gate_success_after_interrupt_is_committed_not_cancelled() -> None:
    token = MutableCancelToken()

    class InterruptAfterCommitTool(RecordingTool):
        def _run(self, input: RiskGateInput, context: ToolContext) -> ToolResult:
            result = super()._run(input, context)
            token.cancelled = True
            return result

    tool = InterruptAfterCommitTool("memory_save")
    registry = ToolRegistry()
    registry.register(tool)
    sink = ListEventSink()

    result = ToolExecutor(
        registry=registry,
        event_sink=sink,
        idempotency_ledger=InMemoryToolIdempotencyLedger(),
        cancel_token=token,
    ).run_tool(
        _realtime_state(run_id="run-1"),
        "step-1",
        tool.name,
        {"text": "保存通勤降噪偏好"},
    )

    assert result.success is True
    assert tool.calls == 1
    assert not [event for event in sink.events if event.type == "tool_failed"]
    finished = next(event for event in sink.events if event.type == "tool_finished")
    post = finished.payload["post_tool_call"]
    assert post["status"] == "succeeded"
    assert post["risk_gate"]["level"] == "hard_gate"
    assert post["side_effect"]["level"] == "committed"
    assert "cancel" not in post


def test_realtime_unknown_tool_defaults_to_confirmation_gate_without_execution() -> None:
    tool = RecordingTool("custom_notification")
    registry = ToolRegistry()
    registry.register(tool)
    ledger = InMemoryToolIdempotencyLedger()
    sink = ListEventSink()

    result = ToolExecutor(registry=registry, event_sink=sink, idempotency_ledger=ledger).run_tool(
        _realtime_state(run_id="run-1"),
        "step-1",
        tool.name,
        {"text": "发给团队", "confirmed": True, "user_id": "forged-user"},
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert result.data["risk_gate"]["level"] == "hard_gate"
    assert tool.calls == 0
    finished = next(event for event in sink.events if event.type == "tool_finished")
    post = finished.payload["post_tool_call"]
    assert post["status"] == "pending_confirmation"
    assert post["risk_gate"]["level"] == "hard_gate"


def test_realtime_pending_tool_keeps_risk_gate_and_idempotency_summary() -> None:
    state = RealtimeTaskState(task_id="rtask:u1:s1", user_id="u1", session_id="s1")

    updated = reduce_realtime_task_state_event(
        state,
        event_type="tool.started",
        payload={
            "tool_name": "image_generation",
            "step_id": "step-1",
            "pre_tool_call": {
                "risk_gate": {
                    "schema_version": "tool_risk_gate_v1",
                    "level": "soft_gate",
                    "side_effect_level": "compensatable",
                    "requires_confirmation": False,
                },
                "idempotency": {
                    "key": "auto:abc",
                    "required": True,
                    "generated": True,
                    "present": True,
                },
            },
        },
    )

    assert updated.pending_tool["risk_gate"] == {
        "schema_version": "tool_risk_gate_v1",
        "level": "soft_gate",
        "side_effect_level": "compensatable",
        "requires_confirmation": False,
    }
    assert updated.pending_tool["idempotency"] == {
        "key": "auto:abc",
        "required": True,
        "generated": True,
        "present": True,
    }


def test_non_realtime_unknown_tool_uses_the_same_conservative_confirmation_gate() -> None:
    tool = RecordingTool("custom_notification")
    registry = ToolRegistry()
    registry.register(tool)
    ledger = InMemoryToolIdempotencyLedger()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="本地任务"))

    result = ToolExecutor(registry=registry, idempotency_ledger=ledger).run_tool(
        state,
        "step-1",
        tool.name,
        {"text": "发给团队", "confirmed": True, "user_id": "forged-user"},
    )

    assert result.success is True
    assert result.data["requires_confirmation"] is True
    assert result.data["risk_gate"]["reason"] == "confirmation_required"
    assert tool.calls == 0


def test_explicit_approval_never_does_not_add_runtime_confirmation() -> None:
    tool = RecordingTool("trusted_local_write")
    tool.policy = ToolPolicyMetadata(
        risk="local_write",
        approval=ApprovalPolicy(mode="never"),
    )
    registry = ToolRegistry()
    registry.register(tool)
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="本地任务"))

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        tool.name,
        {"text": "更新本地状态"},
    )

    assert result.success is True
    assert tool.calls == 1


def test_approval_never_with_required_idempotency_blocks_without_a_key() -> None:
    tool = RecordingTool("trusted_idempotent_write")
    tool.policy = ToolPolicyMetadata(
        risk="local_write",
        approval=ApprovalPolicy(mode="never"),
        execution=ExecutionPolicy(idempotency="required"),
    )
    registry = ToolRegistry()
    registry.register(tool)
    sink = ListEventSink()
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="本地任务"))

    result = ToolExecutor(registry=registry, event_sink=sink).run_tool(
        state,
        "step-1",
        tool.name,
        {"text": "更新本地状态"},
    )

    assert result.success is True
    assert result.data["status"] == "idempotency_key_required"
    assert result.data["requires_confirmation"] is False
    assert tool.calls == 0
    finished = next(event for event in sink.events if event.type == "tool_finished")
    assert finished.payload["post_tool_call"]["status"] == "idempotency_key_required"
