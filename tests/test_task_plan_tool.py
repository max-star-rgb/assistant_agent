from datetime import datetime, timezone

from pydantic import BaseModel

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.durable_tasks import TrustedTaskBinding
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_spec_adapters import tool_spec_to_json_schema
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry, create_default_registry
from assistant_agent.tools.task_plan_tool import TaskPlanSubmitTool


class EchoInput(BaseModel):
    text: str


class EchoTool(MockTool):
    name = "echo"
    description = "echo"
    input_schema = EchoInput
    output_schema = EchoInput

    def _run(self, input: EchoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"text": input.text})


def test_task_plan_tool_schema_is_nested_and_has_no_runtime_identity() -> None:
    registry, _ = _registry_and_service()
    spec = registry.get_spec("task_plan_submit")
    schema = tool_spec_to_json_schema(spec)

    assert spec.input_schema["fields"]
    assert "fields" not in schema
    assert schema["properties"]["plan"]["type"] == "object"
    step_schema = schema["properties"]["plan"]["properties"]["steps"]["items"]
    assert "depends_on" in step_schema["properties"]
    assert "tool_name" in step_schema["properties"]
    assert not {"user_id", "session_id", "task_id", "lease_token"}.intersection(
        schema["properties"]
    )


def test_plan_tool_creates_then_revises_only_bound_task() -> None:
    registry, service = _registry_and_service()
    tool = registry.get("task_plan_submit")
    created = tool.run(
        {"plan": _plan().model_dump(), "revision_reason": "initial"},
        ToolContext(
            run_id="run_1",
            user_id="u1",
            session_id="s1",
            metadata={"durable_task_service": service},
        ),
    )
    bundle = service.store.load(created.data["task"]["task_id"])
    lease = service.claim_next(worker_id="worker_1", now=datetime.now(timezone.utc))
    bound = service.store.load(bundle.task.task_id)
    binding = TrustedTaskBinding(
        task_id=bound.task.task_id,
        task_version=bound.task.version,
        plan_version=bound.task.current_plan_version,
        lease_owner=bound.task.lease_owner,
        lease_token=bound.task.lease_token,
        ready_step_ids=["step_1"],
    )

    revised = tool.run(
        {"plan": _plan("revised").model_dump(), "revision_reason": "new evidence"},
        ToolContext(
            run_id="run_2",
            user_id="u1",
            session_id="s1",
            metadata={
                "durable_task_service": service,
                "durable_task_binding": binding,
            },
        ),
    )

    assert created.success is True
    assert revised.data["task"]["plan_version"] == 2
    assert service.store.load(bundle.task.task_id).plans[-1].plan.goal == "revised"


def test_registry_registers_plan_tool_only_when_enabled_with_service() -> None:
    base = ToolRegistry()
    service = DurableTaskService(store=InMemoryTaskStore(), registry=base)

    disabled = create_default_registry(ProviderConfig(), durable_task_service=service)
    enabled = create_default_registry(
        ProviderConfig(durable_tasks_enabled=True),
        durable_task_service=service,
    )

    assert "task_plan_submit" not in disabled.list()
    assert "task_plan_submit" in enabled.list()
    assert enabled.get_spec("task_plan_submit").execution.dependency_mode == "terminal"


def test_action_validator_enforces_task_execution_mode_and_ready_step() -> None:
    registry, _ = _registry_and_service()
    request = UserRequest(
        user_id="u1", session_id="s1", text="durable", task_execution_mode="durable"
    )
    state = AgentState.from_request(request)
    direct = ActionValidator().validate(
        decision=AssistantDecision(type="tool_call", tool_name="echo", tool_input={"text": "x"}),
        registry=registry,
        request=request,
        state=state,
    )
    plan = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="task_plan_submit",
            tool_input={"plan": _plan().model_dump(), "revision_reason": "initial"},
        ),
        registry=registry,
        request=request,
        state=state,
    )
    foreground_request = request.model_copy(update={"task_execution_mode": "foreground"})
    foreground = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="task_plan_submit",
            tool_input={"plan": _plan().model_dump(), "revision_reason": "initial"},
        ),
        registry=registry,
        request=foreground_request,
        state=AgentState.from_request(foreground_request),
    )

    assert direct.code == "durable_plan_required"
    assert plan.accepted is True
    assert foreground.code == "durable_plan_forbidden"


def _registry_and_service() -> tuple[ToolRegistry, DurableTaskService]:
    registry = ToolRegistry()
    registry.register(EchoTool())
    service = DurableTaskService(store=InMemoryTaskStore(), registry=registry)
    registry.register(TaskPlanSubmitTool(service))
    return registry, service


def _plan(goal: str = "echo") -> TaskPlan:
    return TaskPlan(
        goal=goal,
        steps=[TaskStep(step_id="step_1", action="echo", tool_name="echo")],
    )
