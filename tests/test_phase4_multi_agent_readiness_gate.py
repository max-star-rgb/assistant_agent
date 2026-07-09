from assistant_agent.agent.state import AgentState
from assistant_agent.agent_routing import WORKER_AGENT_ID, AgentRouteRequest, AgentRouter
from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID
from assistant_agent.schemas.planning import IntentResult
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.agent_control_plane import (
    AgentControlPlaneQueryService,
    InMemoryAgentControlPlaneStore,
    JsonlAgentControlPlaneStore,
)
from assistant_agent.services.trace_query import TraceQueryService
from assistant_agent.services.trace_store import InMemoryTraceStore


class DelegatingRuntime:
    def __init__(self, *, run_id: str = "run_phase4_controller", delegate: bool = True) -> None:
        self.run_id = run_id
        self.delegate = delegate
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id=self.run_id)
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="phase4 readiness gate"))
        if self.delegate:
            state.tool_results.append(
                ToolResult(
                    tool_name="delegate_to_agent",
                    success=True,
                    data={
                        "task_id": "agent_task_phase4",
                        "target_agent_id": WORKER_AGENT_ID,
                        "status": "completed",
                        "run_id": "run_phase4_worker",
                        "trace_id": "trace_phase4_worker",
                        "artifacts": [{"kind": "text", "text": "worker summary only"}],
                        "errors": [],
                        "metadata": {
                            "correlation_id": "corr_phase4",
                            "agent_context": {
                                "memory_scope": {"parent_memory_forwarded": False},
                                "omitted_context": [
                                    {
                                        "key": "memory_context_text",
                                        "reason": "memory_context_not_forwarded",
                                    }
                                ],
                            },
                            "raw_provider_response": "raw provider response with sk-secret",
                            "memory_context_text": "private parent memory",
                        },
                    },
                )
            )
        state.set_response(AgentResponse(message="controller handled", data={"agent_id": DEFAULT_AGENT_ID}))
        return state


def test_jsonl_control_plane_store_survives_restart_and_preserves_delegation_trace(tmp_path) -> None:
    store_path = tmp_path / "agent_control_plane.jsonl"
    router = AgentRouter(
        {DEFAULT_AGENT_ID: DelegatingRuntime(), WORKER_AGENT_ID: DelegatingRuntime(run_id="run_unused_worker")},
        control_plane_store=JsonlAgentControlPlaneStore(store_path),
    )

    response = router.run(
        AgentRouteRequest(
            user_id="u1",
            session_id="s1",
            text="coordinate a worker",
            collaboration_mode="controller_delegate",
        )
    )

    restarted_store = JsonlAgentControlPlaneStore(store_path)
    record = restarted_store.get(response.run_id)
    assert record is not None
    assert restarted_store.get_by_trace_id(response.trace_id).run_id == response.run_id
    assert record.delegated_tasks[0]["target_agent_id"] == WORKER_AGENT_ID
    assert record.delegated_tasks[0]["metadata"]["agent_context"]["memory_scope"]["parent_memory_forwarded"] is False

    event_types = {event.event_type for event in restarted_store.list_audit_events(run_id=response.run_id)}
    assert {"route_decision", "delegation_decision"}.issubset(event_types)
    persisted = store_path.read_text(encoding="utf-8")
    assert "private parent memory" not in persisted
    assert "raw provider response" not in persisted
    assert "sk-secret" not in persisted

    query = AgentControlPlaneQueryService(
        trace_query=TraceQueryService(InMemoryTraceStore()),
        router_store=restarted_store,
    )
    audit = query.audit_events_by_run(response.run_id)
    assert audit.retention["durable"] is True
    assert audit.retention["storage"] == "jsonl_file"


def test_default_agent_router_keeps_process_local_control_plane_retention() -> None:
    router = AgentRouter({DEFAULT_AGENT_ID: DelegatingRuntime(delegate=False)})

    response = router.run(AgentRouteRequest(user_id="u1", session_id="s1", text="single agent"))

    assert isinstance(router.control_plane_store, InMemoryAgentControlPlaneStore)
    query = AgentControlPlaneQueryService(
        trace_query=TraceQueryService(InMemoryTraceStore()),
        router_store=router.control_plane_store,
    )
    audit = query.audit_events_by_run(response.run_id)
    assert audit.retention["durable"] is False
    assert audit.retention["storage"] == "process_local_memory"
