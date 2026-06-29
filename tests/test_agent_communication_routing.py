import pytest

from multimodal_agent.agent.action_validator import ActionValidator
from multimodal_agent.agent.state import AgentState
from multimodal_agent.schemas.assistant_decision import AssistantDecision
from multimodal_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentInstance,
    AgentMessage,
    AgentRouteRequest,
    AgentSessionRef,
    AgentTask,
)
from multimodal_agent.schemas.planning import IntentResult
from multimodal_agent.schemas.requests import AgentResponse, UserRequest
from multimodal_agent.services.agent_communication import AgentCommunicationService
from multimodal_agent.services.agent_communication import create_local_agent_communication_service
from multimodal_agent.services.agent_directory import AgentDirectory
from multimodal_agent.services.agent_transports import LocalAgentTransport
from multimodal_agent.tools.agent_delegation_tool import AgentDelegationTool
from multimodal_agent.tools.base import ToolContext
from multimodal_agent.tools.registry import create_default_registry


class RecordingRuntime:
    def __init__(self, *, agent_id: str = DEFAULT_AGENT_ID, run_id: str = "run_agent_comm_test") -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id=self.run_id)
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="agent communication test"))
        state.set_response(
            AgentResponse(
                message=f"handled: {request.text}",
                data={"agent_id": self.agent_id},
                output_refs=["local_output_ref"],
            )
        )
        return state


def test_default_directory_routes_to_agent_default() -> None:
    directory = AgentDirectory()

    resolved = directory.resolve(_route_request())

    assert resolved.status == "routed"
    assert resolved.instance is not None
    assert resolved.instance.agent_id == DEFAULT_AGENT_ID
    assert "local" in resolved.instance.transports
    assert "tool_calling" in resolved.instance.capabilities


def test_directory_returns_structured_error_for_unknown_agent() -> None:
    result = AgentDirectory().resolve(_route_request(target_agent_id="agent.missing"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "agent_not_found"
    assert result.error.recoverable is True


def test_local_transport_runs_target_runtime_and_preserves_identity() -> None:
    runtime = RecordingRuntime()
    transport = LocalAgentTransport({DEFAULT_AGENT_ID: runtime})
    task = AgentTask(
        source_agent_id="agent.caller",
        target_agent_id=DEFAULT_AGENT_ID,
        session=AgentSessionRef(
            user_id="u1",
            session_id="s1",
            parent_run_id="run_parent",
            parent_trace_id="trace_parent",
            correlation_id="corr_1",
        ),
        message=AgentMessage(
            text="你好",
            image_ids=["img1"],
            video_ids=["vid1"],
            metadata={"request_origin": "unit_test"},
        ),
    )

    result = transport.send_task(task)

    assert result.status == "completed"
    assert result.run_id == "run_agent_comm_test"
    assert result.trace_id
    assert result.artifacts[0].text == "handled: 你好"
    assert result.artifacts[0].output_refs == ["local_output_ref"]
    assert result.metadata["transport"] == "local"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "u1"
    assert request.session_id == "s1"
    assert request.image_ids == ["img1"]
    assert request.video_ids == ["vid1"]
    assert request.metadata["request_origin"] == "unit_test"
    assert request.metadata["agent_communication"]["source_agent_id"] == "agent.caller"
    assert request.metadata["agent_communication"]["correlation_id"] == "corr_1"


def test_communication_service_uses_directory_and_local_transport() -> None:
    runtime = RecordingRuntime()
    service = AgentCommunicationService(
        directory=AgentDirectory(),
        transports=[LocalAgentTransport({DEFAULT_AGENT_ID: runtime})],
    )

    result = service.send_message(
        target_agent_id=DEFAULT_AGENT_ID,
        source_agent_id="agent.caller",
        session=AgentSessionRef(user_id="u1", session_id="s1", correlation_id="corr_2"),
        message=AgentMessage(text="交给 default 处理"),
    )

    assert result.status == "completed"
    assert result.artifacts[0].text == "handled: 交给 default 处理"
    assert runtime.requests[0].metadata["agent_communication"]["target_agent_id"] == DEFAULT_AGENT_ID


def test_communication_service_enforces_delegation_depth() -> None:
    runtime = RecordingRuntime()
    service = AgentCommunicationService(
        directory=AgentDirectory(),
        transports=[LocalAgentTransport({DEFAULT_AGENT_ID: runtime})],
    )
    task = AgentTask(
        target_agent_id=DEFAULT_AGENT_ID,
        session=AgentSessionRef(user_id="u1", session_id="s1", correlation_id="corr_3"),
        message=AgentMessage(text="too deep"),
        delegation_depth=2,
        max_delegation_depth=1,
    )

    result = service.send_task(task)

    assert result.status == "failed"
    assert result.errors[0].code == "agent_delegation_depth_exceeded"
    assert runtime.requests == []


def test_communication_service_rejects_disabled_agent() -> None:
    directory = AgentDirectory(
        [
            AgentInstance(
                agent_id=DEFAULT_AGENT_ID,
                display_name="Disabled Default",
                enabled=False,
                transports=["local"],
            )
        ]
    )
    service = AgentCommunicationService(
        directory=directory,
        transports=[LocalAgentTransport({DEFAULT_AGENT_ID: RecordingRuntime()})],
    )

    result = service.send_message(
        target_agent_id=DEFAULT_AGENT_ID,
        session=AgentSessionRef(user_id="u1", session_id="s1"),
        message=AgentMessage(text="hello"),
    )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_disabled"


def test_create_local_agent_communication_service_builds_multi_instance_directory() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default")
    worker_runtime = RecordingRuntime(agent_id="agent.worker", run_id="run_worker")
    service = create_local_agent_communication_service(
        {
            DEFAULT_AGENT_ID: default_runtime,
            "agent.worker": worker_runtime,
        }
    )

    instances = {instance.agent_id: instance for instance in service.directory.list(include_disabled=True)}

    assert set(instances) == {DEFAULT_AGENT_ID, "agent.worker"}
    assert instances[DEFAULT_AGENT_ID].metadata["default"] is True
    assert instances["agent.worker"].metadata["local"] is True
    assert instances["agent.worker"].capabilities == ["chat", "tool_calling"]

    result = service.send_message(
        target_agent_id="agent.worker",
        source_agent_id=DEFAULT_AGENT_ID,
        session=AgentSessionRef(user_id="u1", session_id="s1", correlation_id="corr_factory"),
        message=AgentMessage(text="worker 子任务"),
    )

    assert result.status == "completed"
    assert result.run_id == "run_worker"
    assert result.artifacts[0].data["agent_id"] == "agent.worker"
    assert len(worker_runtime.requests) == 1
    assert default_runtime.requests == []


def test_create_local_agent_communication_service_accepts_explicit_instances() -> None:
    service = create_local_agent_communication_service(
        {DEFAULT_AGENT_ID: RecordingRuntime(), "agent.worker": RecordingRuntime()},
        instances=[
            AgentInstance(
                agent_id="agent.worker",
                display_name="Worker Specialist",
                capabilities=["worker_specialist"],
                transports=["local"],
                metadata={"role": "worker"},
            )
        ],
    )

    worker = service.directory.get("agent.worker")
    default = service.directory.get(DEFAULT_AGENT_ID)
    route = service.directory.resolve(AgentRouteRequest(capability="worker_specialist"))

    assert worker is not None
    assert worker.display_name == "Worker Specialist"
    assert worker.metadata["role"] == "worker"
    assert default is not None
    assert default.metadata["default"] is True
    assert route.status == "routed"
    assert route.instance is not None
    assert route.instance.agent_id == "agent.worker"


def test_create_local_agent_communication_service_requires_runtimes() -> None:
    with pytest.raises(ValueError, match="at least one local runtime"):
        create_local_agent_communication_service({})


def test_default_registry_does_not_register_delegate_to_agent() -> None:
    registry = create_default_registry()

    assert "delegate_to_agent" not in registry.list()


def test_agent_delegation_registry_registration_is_explicit() -> None:
    with pytest.raises(ValueError, match="agent_communication_service is required"):
        create_default_registry(enable_agent_delegation=True)

    service = _worker_service(RecordingRuntime())
    registry = create_default_registry(
        enable_agent_delegation=True,
        agent_communication_service=service,
    )

    assert "delegate_to_agent" in registry.list()
    spec = next(spec for spec in registry.list_specs() if spec.name == "delegate_to_agent")
    assert "Opt-in only" in " ".join(spec.runtime_constraints)


def test_delegate_to_agent_tool_runs_enabled_local_agent() -> None:
    runtime = RecordingRuntime()
    registry = create_default_registry(
        enable_agent_delegation=True,
        agent_communication_service=_worker_service(runtime),
    )

    result = registry.run(
        "delegate_to_agent",
        {"target_agent_id": "agent.worker", "text": "请处理这个子任务"},
        ToolContext(
            run_id="run_parent",
            user_id="u1",
            session_id="s1",
            metadata={"agent_id": DEFAULT_AGENT_ID, "trace_id": "trace_parent"},
        ),
    )

    assert result.success is True
    assert result.tool_name == "delegate_to_agent"
    assert result.output_ref and result.output_ref.startswith("local://agent-task/")
    assert result.contract is not None
    assert result.contract.capability == "agent_delegation"
    assert result.data is not None
    assert result.data["target_agent_id"] == "agent.worker"
    assert result.data["artifacts"][0]["text"] == "handled: 请处理这个子任务"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "u1"
    assert request.session_id == "s1"
    assert request.metadata["agent_communication"]["parent_run_id"] == "run_parent"
    assert request.metadata["agent_communication"]["parent_trace_id"] == "trace_parent"


def test_delegate_to_agent_tool_uses_local_multi_instance_factory() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default")
    worker_runtime = RecordingRuntime(agent_id="agent.worker", run_id="run_worker")
    service = create_local_agent_communication_service(
        {
            DEFAULT_AGENT_ID: default_runtime,
            "agent.worker": worker_runtime,
        }
    )
    registry = create_default_registry(
        enable_agent_delegation=True,
        agent_communication_service=service,
    )

    result = registry.run(
        "delegate_to_agent",
        {"target_agent_id": "agent.worker", "text": "从 default 委托给 worker"},
        ToolContext(
            run_id="run_parent",
            user_id="u1",
            session_id="s1",
            metadata={"agent_id": DEFAULT_AGENT_ID, "trace_id": "trace_parent"},
        ),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["run_id"] == "run_worker"
    assert result.data["artifacts"][0]["data"]["agent_id"] == "agent.worker"
    assert len(worker_runtime.requests) == 1
    assert default_runtime.requests == []


def test_delegate_to_agent_tool_fails_without_service() -> None:
    result = AgentDelegationTool().run(
        {"target_agent_id": "agent.worker", "text": "hello"},
        ToolContext(user_id="u1", session_id="s1"),
    )

    assert result.success is False
    assert result.error == "Agent delegation service is not configured."
    assert result.contract is not None
    assert result.contract.status == "failed"


def test_delegate_to_agent_tool_blocks_self_delegation() -> None:
    result = AgentDelegationTool(_worker_service(RecordingRuntime())).run(
        {"target_agent_id": DEFAULT_AGENT_ID, "text": "hello"},
        ToolContext(user_id="u1", session_id="s1", metadata={"agent_id": DEFAULT_AGENT_ID}),
    )

    assert result.success is False
    assert result.data is not None
    assert result.data["errors"][0]["code"] == "agent_self_delegation_blocked"


def test_action_validator_rejects_empty_agent_delegation_payload() -> None:
    registry = create_default_registry(
        enable_agent_delegation=True,
        agent_communication_service=_worker_service(RecordingRuntime()),
    )
    request = UserRequest(user_id="u1", session_id="s1", text="delegate")
    state = AgentState.from_request(request)

    validation = ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="delegate_to_agent",
            tool_input={"target_agent_id": "agent.worker"},
        ),
        registry=registry,
        request=request,
        state=state,
    )

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"


def _route_request(target_agent_id: str | None = None):
    return AgentRouteRequest(target_agent_id=target_agent_id)


def _worker_service(runtime: RecordingRuntime) -> AgentCommunicationService:
    return AgentCommunicationService(
        directory=AgentDirectory(
            [
                AgentInstance(
                    agent_id="agent.worker",
                    display_name="Worker Agent",
                    capabilities=["chat"],
                    transports=["local"],
                )
            ]
        ),
        transports=[LocalAgentTransport({"agent.worker": runtime})],
    )
