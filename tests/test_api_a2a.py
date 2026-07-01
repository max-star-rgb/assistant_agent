from fastapi.testclient import TestClient

from assistant_agent.api import routes_a2a
from assistant_agent.api.auth import (
    AUTH_HEADER_ENABLED_ENV,
    AUTH_MODE_ENV,
    AUTH_REQUIRE_BOUND_IDENTITY_ENV,
    AUTH_SESSION_ID_HEADER,
    AUTH_USER_ID_HEADER,
)
from assistant_agent.api.app import create_app
from assistant_agent.schemas.a2a import A2AAgentCard, A2ATaskResult
from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID, AgentInstance
from assistant_agent.schemas.api import AgentRunResponse
from assistant_agent.services.agent_directory import AgentDirectory, default_agent_instance


class RecordingGateway:
    def __init__(self, *, failed: bool = False, include_private_card_data: bool = False) -> None:
        self.failed = failed
        self.requests = []
        instances = [
            default_agent_instance(),
            AgentInstance(
                agent_id="agent.worker",
                display_name="Worker Agent",
                description="Local worker test agent.",
                capabilities=["chat", "tool_calling"],
                transports=["local"],
            ),
        ]
        if include_private_card_data:
            instances.append(
                AgentInstance(
                    agent_id="agent.private",
                    display_name="Private Agent",
                    description="ProviderConfig loaded from /home/demo/secret.py",
                    capabilities=["chat", "internal_secret_capability"],
                    transports=["local"],
                )
            )
        self.directory = AgentDirectory(instances)

    def run(self, request) -> AgentRunResponse:
        self.requests.append(request)
        if self.failed:
            return AgentRunResponse(
                run_id="run_a2a_failed",
                trace_id="trace_a2a_failed",
                status="failed",
                intent=None,
                response_text="Agent not found: agent.missing",
                data={"agent_gateway": {"agent_id": None, "target_agent_id": request.target_agent_id}},
            )
        return AgentRunResponse(
            run_id="run_a2a_test",
            trace_id="trace_a2a_test",
            status="completed",
            intent="chat",
            response_text="a2a response",
            data={
                "agent_gateway": {
                    "agent_id": request.target_agent_id or DEFAULT_AGENT_ID,
                    "collaboration_mode": request.effective_collaboration_mode(),
                }
            },
        )


class ExplodingGateway(RecordingGateway):
    def run(self, request) -> AgentRunResponse:
        raise RuntimeError("provider secret should not leak")


def test_a2a_agent_card_exposes_json_rpc_endpoint(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    payload = response.json()
    A2AAgentCard.model_validate(payload)
    assert payload["protocolVersion"] == "1.0.0"
    assert payload["preferredTransport"] == "JSONRPC"
    assert payload["url"] == "http://testserver/a2a/rpc"
    assert payload["authentication"]["required"] is False
    assert payload["supportedMethods"] == ["SendMessage", "message/send"]
    assert {skill["id"] for skill in payload["skills"]} == {DEFAULT_AGENT_ID, "agent.worker"}


def test_a2a_agent_card_filters_private_skill_details(monkeypatch) -> None:
    gateway = RecordingGateway(include_private_card_data=True)
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    serialized = response.text
    assert "ProviderConfig" not in serialized
    assert "/home/demo" not in serialized
    assert "internal_secret_capability" not in serialized
    private_skill = next(skill for skill in response.json()["skills"] if skill["id"] == "agent.private")
    assert private_skill["description"] == "Local agent."
    assert private_skill["tags"] == ["chat", "local"]


def test_a2a_send_message_routes_to_agent_gateway(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "rpc_1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg_1",
                    "contextId": "ctx_1",
                    "parts": [{"kind": "text", "text": "hello from A2A"}],
                    "metadata": {
                        "user_id": "u1",
                        "target_agent_id": "agent.worker",
                    },
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "rpc_1"
    assert payload["error"] is None
    result = payload["result"]
    A2ATaskResult.model_validate(result)
    assert result["id"] == "run_a2a_test"
    assert result["contextId"] == "ctx_1"
    assert result["status"]["state"] == "completed"
    assert result["status"]["message"]["parts"][0]["text"] == "a2a response"
    assert result["metadata"]["trace_id"] == "trace_a2a_test"
    assert result["artifacts"][0]["parts"][0]["text"] == "a2a response"
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.user_id == "u1"
    assert request.session_id == "ctx_1"
    assert request.text == "hello from A2A"
    assert request.target_agent_id == "agent.worker"


def test_a2a_send_message_uses_enabled_header_auth(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        headers={AUTH_USER_ID_HEADER: "auth_user", AUTH_SESSION_ID_HEADER: "header_session"},
        json={
            "jsonrpc": "2.0",
            "id": "rpc_auth",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg_auth",
                    "contextId": "body_context",
                    "parts": [{"kind": "text", "text": "hello from A2A"}],
                    "metadata": {
                        "user_id": "auth_user",
                        "target_agent_id": "agent.worker",
                    },
                }
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["error"] is None
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.user_id == "auth_user"
    assert request.session_id == "header_session"
    metadata = request.metadata["request_identity"]
    assert metadata["identity_source"] == "auth_context"
    assert metadata["auth_bound_identity"] is True
    assert metadata["requested_session_id"] == "body_context"


def test_a2a_send_message_uses_trusted_header_auth_mode_when_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_MODE_ENV, "trusted_header")
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "1")
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        headers={AUTH_USER_ID_HEADER: "auth_user", AUTH_SESSION_ID_HEADER: "trusted_session"},
        json={
            "jsonrpc": "2.0",
            "id": "rpc_trusted_auth",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg_trusted_auth",
                    "contextId": "body_context",
                    "parts": [{"kind": "text", "text": "hello from A2A"}],
                    "metadata": {
                        "user_id": "auth_user",
                        "target_agent_id": "agent.worker",
                    },
                }
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["error"] is None
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.user_id == "auth_user"
    assert request.session_id == "trusted_session"
    assert request.metadata["request_identity"]["auth_context_source"] == "header"


def test_a2a_send_message_rejects_request_identity_when_auth_bound_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_REQUIRE_BOUND_IDENTITY_ENV, "true")
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "rpc_auth_required",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg_auth_required",
                    "contextId": "body_context",
                    "parts": [{"kind": "text", "text": "hello from A2A"}],
                    "metadata": {
                        "user_id": "body_user",
                        "target_agent_id": "agent.worker",
                    },
                }
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["id"] == "rpc_auth_required"
    assert payload["error"]["code"] == -32602
    assert payload["error"]["data"]["code"] == "IDENTITY_NOT_AUTH_BOUND"
    assert gateway.requests == []


def test_a2a_send_message_rejects_enabled_header_auth_user_mismatch(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        headers={AUTH_USER_ID_HEADER: "auth_user"},
        json={
            "jsonrpc": "2.0",
            "id": "rpc_auth_mismatch",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg_auth_mismatch",
                    "contextId": "body_context",
                    "parts": [{"kind": "text", "text": "hello from A2A"}],
                    "metadata": {
                        "user_id": "body_user",
                        "target_agent_id": "agent.worker",
                    },
                }
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["id"] == "rpc_auth_mismatch"
    assert payload["error"]["code"] == -32602
    assert "auth context" in payload["error"]["message"]
    assert gateway.requests == []


def test_a2a_send_message_uses_params_context_id_when_message_context_missing(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "rpc_context",
            "method": "SendMessage",
            "params": {
                "contextId": "ctx_from_params",
                "message": {
                    "role": "user",
                    "messageId": "msg_context",
                    "parts": [{"kind": "text", "text": "context mapping"}],
                    "metadata": {"user_id": "u1"},
                },
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["error"] is None
    assert payload["result"]["contextId"] == "ctx_from_params"
    assert gateway.requests[0].session_id == "ctx_from_params"


def test_a2a_send_message_failed_agent_run_returns_failed_task(monkeypatch) -> None:
    gateway = RecordingGateway(failed=True)
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "rpc_2",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "route missing"}],
                    "metadata": {"user_id": "u1", "targetAgentId": "agent.missing"},
                }
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["error"] is None
    assert payload["result"]["status"]["state"] == "failed"
    assert payload["result"]["metadata"]["runtime_status"] == "failed"
    assert gateway.requests[0].target_agent_id == "agent.missing"


def test_a2a_invalid_json_returns_parse_error(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        content="{",
        headers={"content-type": "application/json"},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["id"] is None
    assert payload["error"]["code"] == -32700
    assert gateway.requests == []


def test_a2a_non_object_request_returns_invalid_request(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post("/a2a/rpc", json=[])

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["id"] is None
    assert payload["error"]["code"] == -32600
    assert gateway.requests == []


def test_a2a_unknown_method_returns_json_rpc_error(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": "rpc_3", "method": "TaskGet", "params": {}},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["error"]["code"] == -32601
    assert gateway.requests == []


def test_a2a_params_must_be_object(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={"jsonrpc": "2.0", "id": "rpc_params", "method": "SendMessage", "params": []},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["id"] == "rpc_params"
    assert payload["error"]["code"] == -32602
    assert gateway.requests == []


def test_a2a_invalid_message_params_return_json_rpc_error(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "rpc_4",
            "method": "SendMessage",
            "params": {"message": {"role": "user", "parts": []}},
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["error"]["code"] == -32602
    assert gateway.requests == []


def test_a2a_gateway_exception_returns_internal_json_rpc_error(monkeypatch) -> None:
    gateway = ExplodingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.post(
        "/a2a/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "rpc_internal",
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "explode"}],
                    "metadata": {"user_id": "u1"},
                }
            },
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["result"] is None
    assert payload["id"] == "rpc_internal"
    assert payload["error"]["code"] == -32603
    assert payload["error"]["message"] == "A2A request failed."
    assert "provider secret" not in response.text
