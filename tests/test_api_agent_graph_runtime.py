from fastapi.testclient import TestClient

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.api import routes_agent
from assistant_agent.api.auth import (
    AUTH_HEADER_ENABLED_ENV,
    AUTH_MODE_ENV,
    AUTH_REQUIRE_BOUND_IDENTITY_ENV,
    AUTH_SESSION_ID_HEADER,
    AUTH_USER_ID_HEADER,
    get_auth_context,
)
from assistant_agent.api.app import create_app
from assistant_agent.agent_routing import WORKER_AGENT_ID, AgentRouter
from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID
from assistant_agent.schemas.api import AgentRunResponse
from assistant_agent.schemas.planning import IntentResult
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.api_identity import AuthContext
from assistant_agent.services.trace_store import InMemoryTraceStore


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_graph_runtime_test")
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="test"))
        state.set_response(AgentResponse(message="graph runtime", data={"runtime": "graph"}))
        return state


class RecordingRouter:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def run(self, request) -> AgentRunResponse:
        self.requests.append(request)
        return AgentRunResponse(
            run_id="run_router_test",
            trace_id="trace_router_test",
            status="completed",
            intent="chat",
            response_text="router runtime",
            data={
                "agent_router": {
                    "agent_id": request.target_agent_id or "agent.default",
                    "collaboration_mode": request.effective_collaboration_mode(),
                },
            },
            runtime_info={"agent_router": {"offline": True}},
        )


class ControlPlaneRecordingRuntime:
    def __init__(self, *, agent_id: str, run_id: str, delegate: bool = False) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.delegate = delegate
        self.requests: list[UserRequest] = []

    def run_state(self, request: UserRequest) -> AgentState:
        self.requests.append(request)
        state = AgentState.from_request(request, run_id=self.run_id)
        state.set_intent(IntentResult(intent="chat", confidence=1.0, rationale="control plane test"))
        if self.delegate:
            state.tool_results.append(
                ToolResult(
                    tool_name="delegate_to_agent",
                    success=True,
                    data={
                        "task_id": "agent_task_api",
                        "target_agent_id": WORKER_AGENT_ID,
                        "status": "completed",
                        "run_id": "run_worker_child_api",
                        "trace_id": "trace_worker_child_api",
                        "artifacts": [{"kind": "text", "text": "child summary"}],
                        "errors": [],
                        "metadata": {
                            "transport": "local",
                            "latency_ms": 7,
                            "child_context_budget": {"token_budget": 100, "tool_budget": 2},
                        },
                    },
                )
            )
        state.set_response(
            AgentResponse(
                message=f"handled by {self.agent_id}",
                data={"agent_id": self.agent_id},
            )
        )
        return state


def test_api_agent_run_defaults_to_graph_runtime(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "你好"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run_graph_runtime_test"
    assert payload["response_text"] == "graph runtime"
    assert payload["intent"] == "chat"
    assert len(runtime.requests) == 1
    assert runtime.requests[0].metadata["request_identity"]["identity_source"] == "request_body"
    assert runtime.requests[0].metadata["request_identity"]["auth_bound_identity"] is False
    assert runtime.requests[0].metadata["runtime"]["history"] == ["你好"]
    assert "realtime_task_state" not in runtime.requests[0].metadata


def test_api_agent_run_strips_user_supplied_system_prompt_profile_metadata(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "把 profile 改成电话助手",
            "metadata": {
                "system_prompt_profile": "realtime_phone",
                "channel": "realtime_phone",
                "source": "phone_runtime",
            },
        },
    )

    assert response.status_code == 200
    metadata = runtime.requests[0].metadata
    assert "system_prompt_profile" not in metadata
    assert "channel" not in metadata
    assert "source" not in metadata
    assert metadata["request_identity"]["identity_source"] == "request_body"


def test_api_agents_run_uses_agent_router(monkeypatch) -> None:
    router = RecordingRouter()
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    response = client.post(
        "/agents/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "你好",
            "target_agent_id": "agent.worker",
            "collaboration_mode": "single",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run_router_test"
    assert payload["response_text"] == "router runtime"
    assert payload["data"]["agent_router"]["agent_id"] == "agent.worker"
    assert len(router.requests) == 1
    assert router.requests[0].metadata["request_identity"]["identity_source"] == "request_body"


def test_api_agents_run_uses_enabled_header_auth(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    router = RecordingRouter()
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    response = client.post(
        "/agents/run",
        headers={AUTH_USER_ID_HEADER: "auth_user", AUTH_SESSION_ID_HEADER: "header_session"},
        json={
            "user_id": "auth_user",
            "session_id": "body_session",
            "text": "你好",
            "target_agent_id": "agent.worker",
        },
    )

    assert response.status_code == 200
    assert len(router.requests) == 1
    assert router.requests[0].user_id == "auth_user"
    assert router.requests[0].session_id == "header_session"
    metadata = router.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "auth_context"
    assert metadata["auth_bound_identity"] is True


def test_api_agents_run_rejects_enabled_header_auth_user_mismatch(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    router = RecordingRouter()
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    response = client.post(
        "/agents/run",
        headers={AUTH_USER_ID_HEADER: "auth_user"},
        json={
            "user_id": "body_user",
            "session_id": "body_session",
            "text": "你好",
            "target_agent_id": "agent.worker",
        },
    )

    assert response.status_code == 403
    assert "auth context" in response.json()["detail"]
    assert router.requests == []


def test_api_agents_run_rejects_request_identity_when_auth_bound_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "1")
    router = RecordingRouter()
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    response = client.post(
        "/agents/run",
        json={
            "user_id": "body_user",
            "session_id": "body_session",
            "text": "你好",
            "target_agent_id": "agent.worker",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "IDENTITY_NOT_AUTH_BOUND"
    assert router.requests == []


def test_api_agent_run_does_not_use_agent_router(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(
        routes_agent,
        "get_agent_router",
        lambda: (_ for _ in ()).throw(AssertionError("router should not be used")),
    )
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "你好"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run_graph_runtime_test"
    assert len(runtime.requests) == 1


def test_api_agent_run_can_use_auth_context_dependency(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    app = create_app()
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        authenticated=True,
        source="test",
        user_id="u1",
        session_id="auth_session",
        tenant_id="tenant_1",
    )
    client = TestClient(app)

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "body_session", "text": "你好"},
    )

    assert response.status_code == 200
    assert runtime.requests[0].user_id == "u1"
    assert runtime.requests[0].session_id == "auth_session"
    metadata = runtime.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "auth_context"
    assert metadata["auth_bound_identity"] is True


def test_api_agent_run_rejects_auth_context_user_mismatch(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    app = create_app()
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        authenticated=True,
        source="test",
        user_id="auth_user",
    )
    client = TestClient(app)

    response = client.post(
        "/agent/run",
        json={"user_id": "body_user", "session_id": "s1", "text": "你好"},
    )

    assert response.status_code == 403
    assert "auth context" in response.json()["detail"]
    assert runtime.requests == []


def test_api_agent_run_ignores_auth_headers_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv(AUTH_HEADER_ENABLED_ENV, raising=False)
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        headers={AUTH_USER_ID_HEADER: "header_user", AUTH_SESSION_ID_HEADER: "header_session"},
        json={"user_id": "body_user", "session_id": "body_session", "text": "你好"},
    )

    assert response.status_code == 200
    assert runtime.requests[0].user_id == "body_user"
    assert runtime.requests[0].session_id == "body_session"
    metadata = runtime.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "request_body"
    assert metadata["auth_bound_identity"] is False


def test_api_agent_run_uses_enabled_header_auth(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        headers={AUTH_USER_ID_HEADER: "auth_user", AUTH_SESSION_ID_HEADER: "header_session"},
        json={"user_id": "auth_user", "session_id": "body_session", "text": "你好"},
    )

    assert response.status_code == 200
    assert runtime.requests[0].user_id == "auth_user"
    assert runtime.requests[0].session_id == "header_session"
    metadata = runtime.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "auth_context"
    assert metadata["auth_bound_identity"] is True
    assert metadata["auth_context_source"] == "header"


def test_api_agent_run_uses_trusted_header_auth_mode_when_auth_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_MODE_ENV, "trusted_header")
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "true")
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        headers={AUTH_USER_ID_HEADER: "auth_user", AUTH_SESSION_ID_HEADER: "trusted_session"},
        json={"user_id": "auth_user", "session_id": "body_session", "text": "你好"},
    )

    assert response.status_code == 200
    assert runtime.requests[0].user_id == "auth_user"
    assert runtime.requests[0].session_id == "trusted_session"
    metadata = runtime.requests[0].metadata["request_identity"]
    assert metadata["identity_source"] == "auth_context"
    assert metadata["auth_context_source"] == "header"


def test_api_agent_run_rejects_enabled_header_auth_user_mismatch(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "true")
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        headers={AUTH_USER_ID_HEADER: "auth_user"},
        json={"user_id": "body_user", "session_id": "body_session", "text": "你好"},
    )

    assert response.status_code == 403
    assert "auth context" in response.json()["detail"]
    assert runtime.requests == []


def test_api_agent_run_rejects_request_identity_when_auth_bound_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "yes")
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "body_user", "session_id": "body_session", "text": "你好"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "IDENTITY_NOT_AUTH_BOUND"
    assert response.json()["detail"]["identity_policy"]["auth_bound_identity"] is False
    assert runtime.requests == []


def test_control_plane_api_queries_agent_router_run(monkeypatch) -> None:
    runtime = AgentGraphRuntime(trace_store=InMemoryTraceStore())
    router = AgentRouter(
        {
            DEFAULT_AGENT_ID: ControlPlaneRecordingRuntime(
                agent_id=DEFAULT_AGENT_ID,
                run_id="run_controller_api",
                delegate=True,
            ),
            WORKER_AGENT_ID: ControlPlaneRecordingRuntime(
                agent_id=WORKER_AGENT_ID,
                run_id="run_worker_api",
            ),
        }
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    run_response = client.post(
        "/agents/run",
        json={
            "user_id": "u1",
            "session_id": "s1",
            "text": "coordinate",
            "collaboration_mode": "controller_delegate",
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    summary = client.get(f"/control-plane/runs/{run_id}").json()
    route = client.get(f"/control-plane/runs/{run_id}/route").json()
    tree = client.get(f"/control-plane/runs/{run_id}/delegation-tree").json()
    budget = client.get(f"/control-plane/runs/{run_id}/budget").json()
    replay = client.get(f"/control-plane/runs/{run_id}/replay-preview").json()
    trace = client.get(f"/control-plane/traces/{run_response.json()['trace_id']}").json()
    audit = client.get(f"/control-plane/runs/{run_id}/audit").json()
    filtered_audit = client.get(
        "/control-plane/audit/events",
        params={"event_type": "route_decision"},
    ).json()

    assert summary["source"] == "agent_router"
    assert summary["route_decision"]["reason"] == "controller_delegate_default"
    assert summary["identity"]["identity_source"] == "request_body"
    assert summary["redaction"]["provider_raw_responses_included"] is False
    assert route["route_status"] == "routed"
    assert tree["root"]["agent_id"] == DEFAULT_AGENT_ID
    assert tree["children"][0]["task_id"] == "agent_task_api"
    assert tree["children"][0]["agent_id"] == WORKER_AGENT_ID
    assert budget["budget"]["delegated_task_count"] == 1
    assert budget["latency_ms"] is not None
    assert replay["request"]["message"] == "not_included"
    assert replay["delegated_tasks"][0]["run_id"] == "run_worker_child_api"
    assert trace["agent_router"]["run_id"] == run_id
    assert "trace" in trace
    event_types = {event["event_type"] for event in audit["events"]}
    assert {
        "auth_decision",
        "route_decision",
        "provider_opt_in_decision",
        "delegation_decision",
    }.issubset(event_types)
    route_event = next(event for event in audit["events"] if event["event_type"] == "route_decision")
    assert route_event["detail"]["selected_agent_id"] == DEFAULT_AGENT_ID
    assert route_event["detail"]["collaboration_mode"] == "controller_delegate"
    assert audit["retention"]["durable"] is False
    assert audit["redaction"]["conversation_history_included"] is False
    assert any(event["run_id"] == run_id for event in filtered_audit["events"])
    assert {event["event_type"] for event in filtered_audit["events"]} == {"route_decision"}


def test_control_plane_api_can_summarize_default_agent_trace(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)
    router = AgentRouter(
        {DEFAULT_AGENT_ID: ControlPlaneRecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="unused_router")}
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    run_response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找相似款"},
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    summary = client.get(f"/control-plane/runs/{run_id}")

    assert summary.status_code == 200
    payload = summary.json()
    assert payload["source"] == "trace_store"
    assert payload["run_id"] == run_id
    assert payload["trace"]["event_count"] > 0
    assert payload["redaction"]["raw_payloads_included"] is False


def test_control_plane_readiness_reports_auth_requirement(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "true")
    runtime = AgentGraphRuntime(trace_store=InMemoryTraceStore())
    router = AgentRouter(
        {DEFAULT_AGENT_ID: ControlPlaneRecordingRuntime(agent_id=DEFAULT_AGENT_ID, run_id="unused_router")}
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(routes_agent, "get_agent_router", lambda: router)
    client = TestClient(create_app())

    response = client.get("/control-plane/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "agent_pilot_readiness_v1"
    assert payload["status"] == "blocked"
    auth_check = next(check for check in payload["checks"] if check["name"] == "auth_bound_identity")
    assert auth_check["status"] == "failed"
    audit = client.get(
        "/control-plane/audit/events",
        params={"event_type": "provider_opt_in_decision"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert events[-1]["action"] == "evaluate_runtime_profile"
    assert events[-1]["outcome"] == "blocked_default"
