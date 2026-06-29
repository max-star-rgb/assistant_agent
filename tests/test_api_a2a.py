from fastapi.testclient import TestClient

from multimodal_agent.api import routes_a2a
from multimodal_agent.api.app import create_app
from multimodal_agent.schemas.agent_communication import DEFAULT_AGENT_ID, AgentInstance
from multimodal_agent.schemas.api import AgentRunResponse
from multimodal_agent.services.agent_directory import AgentDirectory, default_agent_instance


class RecordingGateway:
    def __init__(self, *, failed: bool = False) -> None:
        self.failed = failed
        self.requests = []
        self.directory = AgentDirectory(
            [
                default_agent_instance(),
                AgentInstance(
                    agent_id="agent.worker",
                    display_name="Worker Agent",
                    description="Local worker test agent.",
                    capabilities=["chat", "tool_calling"],
                    transports=["local"],
                ),
            ]
        )

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


def test_a2a_agent_card_exposes_json_rpc_endpoint(monkeypatch) -> None:
    gateway = RecordingGateway()
    monkeypatch.setattr(routes_a2a, "get_agent_gateway", lambda: gateway)
    client = TestClient(create_app())

    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["protocolVersion"] == "1.0.0"
    assert payload["preferredTransport"] == "JSONRPC"
    assert payload["url"] == "http://testserver/a2a/rpc"
    assert {skill["id"] for skill in payload["skills"]} == {DEFAULT_AGENT_ID, "agent.worker"}


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
    assert result["id"] == "run_a2a_test"
    assert result["contextId"] == "ctx_1"
    assert result["status"]["state"] == "completed"
    assert result["status"]["message"]["parts"][0]["text"] == "a2a response"
    assert result["metadata"]["trace_id"] == "trace_a2a_test"
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.user_id == "u1"
    assert request.session_id == "ctx_1"
    assert request.text == "hello from A2A"
    assert request.target_agent_id == "agent.worker"


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
