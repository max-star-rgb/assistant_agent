from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID, AgentInstance
from assistant_agent.schemas.agent_gateway import AgentGatewayRunRequest
from assistant_agent.schemas.planning import IntentResult
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.agent_directory import AgentDirectory, default_agent_instance
from assistant_agent.services.agent_gateway import WORKER_AGENT_ID, AgentGateway, create_default_agent_gateway


class RecordingRuntime:
    def __init__(
        self,
        *,
        agent_id: str = DEFAULT_AGENT_ID,
        run_id: str = "run_agent_gateway_test",
        include_delegation_result: bool = False,
        delegation_metadata: dict | None = None,
        delegation_errors: list[dict] | None = None,
        delegation_status: str = "completed",
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.include_delegation_result = include_delegation_result
        self.delegation_metadata = delegation_metadata or {}
        self.delegation_errors = delegation_errors or []
        self.delegation_status = delegation_status
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id=self.run_id)
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="agent gateway test"))
        if self.include_delegation_result:
            state.tool_results.append(
                ToolResult(
                    tool_name="delegate_to_agent",
                    success=True,
                    data={
                        "task_id": "agent_task_test",
                        "target_agent_id": WORKER_AGENT_ID,
                        "status": self.delegation_status,
                        "run_id": "run_worker_child",
                        "trace_id": "trace_worker_child",
                        "artifacts": [{"kind": "text", "text": "worker done"}],
                        "errors": self.delegation_errors,
                        "metadata": self.delegation_metadata,
                    },
                )
            )
        state.set_response(
            AgentResponse(
                message=f"handled by {self.agent_id}: {request.text}",
                data={"agent_id": self.agent_id},
            )
        )
        return state


def test_gateway_defaults_to_agent_default() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default")
    worker_runtime = RecordingRuntime(agent_id=WORKER_AGENT_ID, run_id="run_worker")
    gateway = AgentGateway({DEFAULT_AGENT_ID: default_runtime, WORKER_AGENT_ID: worker_runtime})

    response = gateway.run(AgentGatewayRunRequest(user_id="u1", session_id="s1", text="hello"))

    assert response.run_id == "run_default"
    assert response.response_text == "handled by agent.default: hello"
    assert response.data["agent_gateway"]["agent_id"] == DEFAULT_AGENT_ID
    assert response.data["agent_gateway"]["route_decision"]["reason"] == "default_agent"
    assert response.data["agent_gateway"]["route_decision"]["status"] == "routed"
    assert response.runtime_info["agent_gateway"]["collaboration_mode"] == "single"
    assert len(default_runtime.requests) == 1
    assert worker_runtime.requests == []


def test_gateway_routes_explicit_target_agent_to_worker() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default")
    worker_runtime = RecordingRuntime(agent_id=WORKER_AGENT_ID, run_id="run_worker")
    gateway = AgentGateway({DEFAULT_AGENT_ID: default_runtime, WORKER_AGENT_ID: worker_runtime})

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="worker please",
            target_agent_id=WORKER_AGENT_ID,
        )
    )

    assert response.run_id == "run_worker"
    assert response.data["agent_gateway"]["agent_id"] == WORKER_AGENT_ID
    assert response.data["agent_gateway"]["route_decision"]["reason"] == "explicit_target_agent_id"
    assert len(worker_runtime.requests) == 1
    assert default_runtime.requests == []
    record = gateway.control_plane_store.get(response.run_id)
    assert record is not None
    assert record.route_decision["reason"] == "explicit_target_agent_id"
    assert record.route_decision["selected_agent_id"] == WORKER_AGENT_ID
    assert record.identity == {}
    assert record.redaction["raw_payloads_included"] is False


def test_gateway_routes_unique_capability_to_worker() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default")
    worker_runtime = RecordingRuntime(agent_id=WORKER_AGENT_ID, run_id="run_worker")
    gateway = AgentGateway(
        {DEFAULT_AGENT_ID: default_runtime, WORKER_AGENT_ID: worker_runtime},
        directory=AgentDirectory(
            [
                default_agent_instance(),
                AgentInstance(
                    agent_id=WORKER_AGENT_ID,
                    display_name="Worker Agent",
                    capabilities=["worker_specialist"],
                    transports=["local"],
                ),
            ]
        ),
    )

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="route by capability",
            capability="worker_specialist",
        )
    )

    assert response.run_id == "run_worker"
    assert response.data["agent_gateway"]["route_decision"]["reason"] == "capability_match"
    assert response.data["agent_gateway"]["route_decision"]["requested_capability"] == "worker_specialist"
    assert len(worker_runtime.requests) == 1
    assert default_runtime.requests == []


def test_gateway_routes_configured_routing_table_to_worker() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default")
    worker_runtime = RecordingRuntime(agent_id=WORKER_AGENT_ID, run_id="run_worker")
    other_runtime = RecordingRuntime(agent_id="agent.other", run_id="run_other")
    gateway = AgentGateway(
        {
            DEFAULT_AGENT_ID: default_runtime,
            WORKER_AGENT_ID: worker_runtime,
            "agent.other": other_runtime,
        },
        directory=AgentDirectory(
            [
                default_agent_instance(),
                AgentInstance(
                    agent_id=WORKER_AGENT_ID,
                    display_name="Worker Agent",
                    capabilities=["worker_specialist"],
                    transports=["local"],
                ),
                AgentInstance(
                    agent_id="agent.other",
                    display_name="Other Agent",
                    capabilities=["worker_specialist"],
                    transports=["local"],
                ),
            ]
        ),
        routing_table={"worker_specialist": WORKER_AGENT_ID},
    )

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="route by table",
            capability="worker_specialist",
        )
    )

    assert response.run_id == "run_worker"
    assert response.data["agent_gateway"]["route_decision"]["reason"] == "routing_table"
    assert len(worker_runtime.requests) == 1
    assert default_runtime.requests == []
    assert other_runtime.requests == []


def test_gateway_controller_delegate_enters_default_controller() -> None:
    default_runtime = RecordingRuntime(
        agent_id=DEFAULT_AGENT_ID,
        run_id="run_default",
        include_delegation_result=True,
    )
    worker_runtime = RecordingRuntime(agent_id=WORKER_AGENT_ID, run_id="run_worker")
    gateway = AgentGateway({DEFAULT_AGENT_ID: default_runtime, WORKER_AGENT_ID: worker_runtime})

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="coordinate this",
            collaboration_mode="controller_delegate",
        )
    )

    assert response.run_id == "run_default"
    assert response.data["agent_gateway"]["agent_id"] == DEFAULT_AGENT_ID
    assert response.data["agent_gateway"]["collaboration_mode"] == "controller_delegate"
    assert response.data["agent_gateway"]["route_decision"]["reason"] == "controller_delegate_default"
    assert response.data["agent_gateway"]["route_decision"]["delegation_enabled"] is False
    assert response.data["agent_gateway"]["delegated_tasks"] == [
        {
            "task_id": "agent_task_test",
            "target_agent_id": WORKER_AGENT_ID,
            "status": "completed",
            "run_id": "run_worker_child",
            "trace_id": "trace_worker_child",
            "artifact_count": 1,
            "error_codes": [],
        }
    ]
    assert default_runtime.requests[0].metadata["agent_gateway"]["collaboration_mode"] == "controller_delegate"
    assert worker_runtime.requests == []
    record = gateway.control_plane_store.get(response.run_id)
    assert record is not None
    assert record.delegated_tasks[0]["task_id"] == "agent_task_test"
    assert record.budget["delegated_task_count"] == 1
    assert record.failure_class is None
    events = gateway.control_plane_store.list_audit_events(run_id=response.run_id)
    event_types = {event.event_type for event in events}
    assert {
        "auth_decision",
        "route_decision",
        "provider_opt_in_decision",
        "delegation_decision",
    }.issubset(event_types)
    route_event = next(event for event in events if event.event_type == "route_decision")
    assert route_event.detail["selected_agent_id"] == DEFAULT_AGENT_ID
    assert route_event.detail["collaboration_mode"] == "controller_delegate"
    delegation_event = next(event for event in events if event.event_type == "delegation_decision")
    assert delegation_event.detail["target_agent_id"] == WORKER_AGENT_ID
    assert delegation_event.redaction["provider_raw_responses_included"] is False


def test_gateway_records_remote_a2a_audit_event_from_delegation_metadata() -> None:
    default_runtime = RecordingRuntime(
        agent_id=DEFAULT_AGENT_ID,
        run_id="run_default",
        include_delegation_result=True,
        delegation_status="failed",
        delegation_errors=[{"code": "agent_remote_timeout", "message": "Bearer sk-secret timed out"}],
        delegation_metadata={
            "transport": "a2a_json_rpc",
            "endpoint_host": "remote.example.test",
            "remote_status_state": "failed",
            "correlation_id": "corr_remote",
        },
    )
    gateway = AgentGateway({DEFAULT_AGENT_ID: default_runtime})

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="remote worker",
            collaboration_mode="controller_delegate",
        )
    )

    events = gateway.control_plane_store.list_audit_events(run_id=response.run_id, event_type="remote_a2a_decision")
    assert len(events) == 1
    event = events[0]
    assert event.outcome == "blocked"
    assert event.correlation_id == "corr_remote"
    assert event.detail["transport"] == "a2a_json_rpc"
    assert event.detail["endpoint_host"] == "remote.example.test"
    assert event.detail["error_codes"] == ["agent_remote_timeout"]
    assert "sk-secret" not in event.model_dump_json()


def test_gateway_controller_delegate_uses_separate_controller_runtime() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default_single")
    controller_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_default_controller")
    gateway = AgentGateway(
        {DEFAULT_AGENT_ID: default_runtime},
        controller_runtime=controller_runtime,
    )

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="coordinate this",
            collaboration_mode="controller_delegate",
        )
    )

    assert response.run_id == "run_default_controller"
    assert default_runtime.requests == []
    assert len(controller_runtime.requests) == 1


def test_default_gateway_registers_delegation_only_on_controller() -> None:
    gateway = create_default_agent_gateway(config=ProviderConfig.from_env({}), load_env=False)

    assert "delegate_to_agent" in gateway.controller_runtime.registry.list()
    assert "delegate_to_agent" not in gateway.runtimes[DEFAULT_AGENT_ID].registry.list()
    assert "delegate_to_agent" not in gateway.runtimes[WORKER_AGENT_ID].registry.list()


def test_gateway_returns_structured_error_for_unknown_agent() -> None:
    default_runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID)
    gateway = AgentGateway({DEFAULT_AGENT_ID: default_runtime})

    response = gateway.run(
        AgentGatewayRunRequest(
            user_id="u1",
            session_id="s1",
            text="hello",
            target_agent_id="agent.missing",
        )
    )

    assert response.status == "failed"
    assert response.errors[0].code == "AGENT_NOT_FOUND"
    assert response.errors[0].recoverable is True
    assert response.data["agent_gateway"]["target_agent_id"] == "agent.missing"
    assert response.data["agent_gateway"]["route_decision"]["reason"] == "explicit_target_agent_id"
    assert response.data["agent_gateway"]["route_decision"]["status"] == "failed"
    assert response.data["agent_gateway"]["route_decision"]["error_code"] == "agent_not_found"
    assert default_runtime.requests == []
    record = gateway.control_plane_store.get(response.run_id)
    assert record is not None
    assert record.status == "failed"
    assert record.failure_class == "gateway_failure"


def test_gateway_single_agent_configuration_runs_like_default_runtime() -> None:
    runtime = RecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="run_single")
    gateway = AgentGateway({DEFAULT_AGENT_ID: runtime})

    response = gateway.run(AgentGatewayRunRequest(user_id="u1", session_id="s1", text="single"))

    assert response.run_id == "run_single"
    assert response.status == "completed"
    assert response.response_text == "handled by agent.default: single"
    assert len(runtime.requests) == 1
