import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from assistant_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentInstance,
    AgentMessage,
    AgentSessionRef,
    AgentTask,
)
from assistant_agent.services.agent_communication import AgentCommunicationService
from assistant_agent.services.agent_directory import AgentDirectory
from assistant_agent.services.agent_transports import (
    A2AJsonRpcTransport,
    RemoteAgentAllowlist,
)


REMOTE_AGENT_ID = "agent.remote"


def test_a2a_json_rpc_transport_requires_allowlist() -> None:
    result = A2AJsonRpcTransport().send_task(
        _task(),
        instance=_remote_instance("https://remote.example/a2a/rpc"),
    )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_allowlist_missing"
    assert result.metadata["transport"] == "a2a_json_rpc"


def test_a2a_json_rpc_transport_rejects_host_not_allowlisted() -> None:
    transport = A2AJsonRpcTransport(allowlist=RemoteAgentAllowlist(["allowed.example"]))

    result = transport.send_task(_task(), instance=_remote_instance("https://blocked.example/a2a/rpc"))

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_host_not_allowed"


def test_a2a_json_rpc_transport_requires_https_except_localhost_opt_in() -> None:
    transport = A2AJsonRpcTransport(allowlist=RemoteAgentAllowlist(["example.com"]))

    result = transport.send_task(_task(), instance=_remote_instance("http://example.com/a2a/rpc"))

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_https_required"


def test_a2a_json_rpc_transport_sends_task_through_service_to_fake_server() -> None:
    with fake_a2a_server(_success_response) as server:
        service = _remote_service(server.url)

        result = service.send_message(
            target_agent_id=REMOTE_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(user_id="u1", session_id="s1", correlation_id="corr_remote"),
            message=AgentMessage(text="remote task", metadata={"context_refs": ["ctx_1"]}),
            token_budget=100,
            tool_budget=2,
        )

    assert result.status == "completed"
    assert result.run_id == "remote_task_1"
    assert result.trace_id == "remote_trace_1"
    assert result.artifacts[0].text == "remote artifact"
    assert result.metadata["transport"] == "a2a_json_rpc"
    assert result.metadata["endpoint_host"].startswith("127.0.0.1:")
    assert result.metadata["remote_task_id"] == "remote_task_1"
    assert result.metadata["delegation_audit"][0]["event_type"] == "delegation_dispatched"
    assert server.requests[0]["method"] == "message/send"
    message = server.requests[0]["params"]["message"]
    assert message["contextId"] == "s1"
    assert message["parts"] == [{"kind": "text", "text": "remote task"}]
    assert message["metadata"]["user_id"] == "u1"
    assert message["metadata"]["token_budget"] == 100
    assert server.requests[0]["params"]["metadata"]["agent_context"]["context_refs"] == ["ctx_1"]


def test_a2a_json_rpc_transport_validates_agent_card_when_required() -> None:
    with fake_a2a_server(_success_response) as server:
        result = _remote_service(server.url, require_agent_card=True).send_message(
            target_agent_id=REMOTE_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(user_id="u1", session_id="s1"),
            message=AgentMessage(text="card validated"),
        )

    assert result.status == "completed"
    assert len(server.card_requests) == 1
    assert len(server.requests) == 1


def test_a2a_json_rpc_transport_rejects_mismatched_agent_card_endpoint() -> None:
    with fake_a2a_server(
        _success_response,
        card_payload={
            "name": "Remote Agent",
            "url": "https://other.example/a2a/rpc",
            "supportedMethods": ["message/send"],
        },
    ) as server:
        result = _remote_service(server.url, require_agent_card=True).send_message(
            target_agent_id=REMOTE_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(user_id="u1", session_id="s1"),
            message=AgentMessage(text="card mismatch"),
        )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_card_endpoint_mismatch"
    assert len(server.card_requests) == 1
    assert server.requests == []


def test_a2a_json_rpc_transport_normalizes_remote_business_failure() -> None:
    with fake_a2a_server(_business_failure_response) as server:
        result = _remote_service(server.url).send_message(
            target_agent_id=REMOTE_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(user_id="u1", session_id="s1"),
            message=AgentMessage(text="remote failure"),
        )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_business_failed"
    assert result.errors[0].message == "remote failed"
    assert result.metadata["remote_status_state"] == "failed"


def test_a2a_json_rpc_transport_normalizes_json_rpc_protocol_error() -> None:
    with fake_a2a_server(_json_rpc_error_response) as server:
        result = _remote_service(server.url).send_message(
            target_agent_id=REMOTE_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(user_id="u1", session_id="s1"),
            message=AgentMessage(text="bad protocol"),
        )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_protocol_error"
    assert result.errors[0].detail["code"] == -32601


def test_a2a_json_rpc_transport_returns_timeout_error() -> None:
    with fake_a2a_server(_success_response, delay_seconds=0.2) as server:
        result = _remote_service(server.url).send_message(
            target_agent_id=REMOTE_AGENT_ID,
            source_agent_id=DEFAULT_AGENT_ID,
            session=AgentSessionRef(user_id="u1", session_id="s1"),
            message=AgentMessage(text="timeout"),
            timeout_ms=1,
        )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_timeout"


def test_a2a_json_rpc_transport_rejects_payload_over_limit() -> None:
    transport = A2AJsonRpcTransport(
        allowlist=RemoteAgentAllowlist(["remote.example"]),
        max_payload_bytes=100,
    )

    result = transport.send_task(
        _task(text="x" * 200),
        instance=_remote_instance("https://remote.example/a2a/rpc"),
    )

    assert result.status == "failed"
    assert result.errors[0].code == "agent_remote_payload_too_large"


def _task(*, text: str = "hello remote") -> AgentTask:
    return AgentTask(
        source_agent_id=DEFAULT_AGENT_ID,
        target_agent_id=REMOTE_AGENT_ID,
        session=AgentSessionRef(user_id="u1", session_id="s1", correlation_id="corr_test"),
        message=AgentMessage(text=text),
    )


def _remote_instance(endpoint_url: str) -> AgentInstance:
    return AgentInstance(
        agent_id=REMOTE_AGENT_ID,
        display_name="Remote Agent",
        transports=["a2a_json_rpc"],
        endpoint_url=endpoint_url,
    )


def _remote_service(endpoint_url: str, *, require_agent_card: bool = False) -> AgentCommunicationService:
    parsed_host = endpoint_url.removeprefix("http://").split("/", 1)[0]
    return AgentCommunicationService(
        directory=AgentDirectory(
            [
                AgentInstance(
                    agent_id=DEFAULT_AGENT_ID,
                    display_name="Default Agent",
                    role="controller",
                    can_delegate=True,
                    allowed_targets=[REMOTE_AGENT_ID],
                    transports=["local"],
                ),
                AgentInstance(
                    agent_id=REMOTE_AGENT_ID,
                    display_name="Remote Agent",
                    role="worker",
                    transports=["a2a_json_rpc"],
                    endpoint_url=endpoint_url,
                ),
            ]
        ),
        transports=[
            A2AJsonRpcTransport(
                allowlist=RemoteAgentAllowlist([parsed_host], allow_http_localhost=True),
                require_agent_card=require_agent_card,
            )
        ],
    )


@contextmanager
def fake_a2a_server(
    response_factory,
    *,
    delay_seconds: float = 0.0,
    card_payload: dict[str, Any] | None = None,
):
    requests: list[dict[str, Any]] = []
    card_requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            card_requests.append(self.path)
            payload = card_payload or {
                "name": "Remote Agent",
                "url": f"http://127.0.0.1:{self.server.server_address[1]}/a2a/rpc",
                "supportedMethods": ["message/send"],
            }
            response_body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or "0")
            request_payload = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append(request_payload)
            if delay_seconds:
                time.sleep(delay_seconds)
            response_payload = response_factory(request_payload)
            response_body = json.dumps(response_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response_body)))
            self.end_headers()
            try:
                self.wfile.write(response_body)
            except BrokenPipeError:
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.url = f"http://127.0.0.1:{server.server_address[1]}/a2a/rpc"
    server.requests = requests
    server.card_requests = card_requests
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _success_response(request_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_payload["id"],
        "result": {
            "id": "remote_task_1",
            "contextId": "s1",
            "status": {
                "state": "completed",
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "remote completed"}],
                },
            },
            "artifacts": [
                {
                    "artifactId": "artifact_1",
                    "name": "remote-output",
                    "parts": [{"kind": "text", "text": "remote artifact"}],
                }
            ],
            "metadata": {"trace_id": "remote_trace_1"},
        },
    }


def _business_failure_response(request_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_payload["id"],
        "result": {
            "id": "remote_task_failed",
            "contextId": "s1",
            "status": {
                "state": "failed",
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "remote failed"}],
                },
            },
            "metadata": {"trace_id": "remote_trace_failed"},
        },
    }


def _json_rpc_error_response(request_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_payload["id"],
        "error": {"code": -32601, "message": "method not found"},
    }
