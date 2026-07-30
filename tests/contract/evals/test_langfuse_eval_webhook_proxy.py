from __future__ import annotations

import hashlib
import hmac
from contextlib import contextmanager
from http.client import HTTPConnection, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Iterator


class _RecordingUpstreamHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append(
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type"),
                "signature": self.headers.get("x-langfuse-signature"),
            }
        )
        response = b'{"status":"accepted"}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _running_server(
    server: ThreadingHTTPServer,
) -> Iterator[ThreadingHTTPServer]:
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_preserves_signed_remote_experiment_request() -> None:
    from deploy.langfuse_eval_webhook.webhook_proxy import (
        WebhookProxyConfig,
        create_server,
    )

    _RecordingUpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _RecordingUpstreamHandler,
    )
    upstream_port = int(upstream.server_address[1])
    proxy = create_server(
        WebhookProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="127.0.0.1",
            upstream_port=upstream_port,
        )
    )

    with _running_server(upstream), _running_server(proxy):
        connection = HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
        body = (
            '{"datasetName":"回归集",'
            '"payload":"{\\"task\\":\\"email_empty_result_honesty\\"}"}'
        ).encode()
        connection.request(
            "POST",
            "/internal/evals/langfuse/remote-experiment",
            body=body,
            headers={
                "Content-Type": "application/json",
                "x-langfuse-signature": "t=1722222222,v1=abc123",
            },
        )
        response = connection.getresponse()
        response_body = response.read()
        connection.close()

    assert response.status == 202
    assert response.getheader("Content-Type") == "application/json"
    assert response_body == b'{"status":"accepted"}'
    assert _RecordingUpstreamHandler.requests == [
        {
            "path": "/internal/evals/langfuse/remote-experiment",
            "body": body,
            "content_type": "application/json",
            "signature": "t=1722222222,v1=abc123",
        }
    ]


def test_proxy_signs_unsigned_langfuse_32242_request() -> None:
    from deploy.langfuse_eval_webhook.webhook_proxy import (
        WebhookProxyConfig,
        create_server,
    )

    _RecordingUpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _RecordingUpstreamHandler,
    )
    body = (
        b'{"projectId":"project-test-id","datasetId":"dataset-test-id",'
        b'"datasetName":"assistant-agent-regression",'
        b'"payload":"{\\"suite\\":\\"release\\"}"}'
    )
    timestamp = 1_800_000_000
    secret = "proxy-signing-secret"
    expected_signature = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    proxy = create_server(
        WebhookProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="127.0.0.1",
            upstream_port=int(upstream.server_address[1]),
            signing_secret=secret,
            now=lambda: timestamp,
        )
    )

    with _running_server(upstream), _running_server(proxy):
        connection = HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
        connection.request(
            "POST",
            "/internal/evals/langfuse/remote-experiment",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()

    assert response.status == 202
    assert _RecordingUpstreamHandler.requests[0]["body"] == body
    assert _RecordingUpstreamHandler.requests[0]["signature"] == (
        f"t={timestamp},v1={expected_signature}"
    )


def test_proxy_rejects_non_webhook_paths_without_contacting_upstream() -> None:
    from deploy.langfuse_eval_webhook.webhook_proxy import (
        WebhookProxyConfig,
        create_server,
    )

    _RecordingUpstreamHandler.requests = []
    upstream = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _RecordingUpstreamHandler,
    )
    proxy = create_server(
        WebhookProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="127.0.0.1",
            upstream_port=int(upstream.server_address[1]),
        )
    )

    with _running_server(upstream), _running_server(proxy):
        connection = HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
        connection.request(
            "POST",
            "/api/admin",
            body=b'{"unexpected":true}',
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()

    assert response.status == 404
    assert _RecordingUpstreamHandler.requests == []


def test_proxy_returns_bad_gateway_when_assistant_server_is_unavailable() -> None:
    from deploy.langfuse_eval_webhook.webhook_proxy import (
        WebhookProxyConfig,
        create_server,
    )

    proxy = create_server(
        WebhookProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="127.0.0.1",
            upstream_port=0,
        )
    )

    response_status: int | None = None
    response_body = b""
    with _running_server(proxy):
        connection = HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
        connection.request(
            "POST",
            "/internal/evals/langfuse/remote-experiment",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        try:
            response = connection.getresponse()
        except RemoteDisconnected:
            pass
        else:
            response_status = response.status
            response_body = response.read()
        finally:
            connection.close()

    assert response_status == 502
    assert response_body == b'{"error":"assistant_server_unavailable"}'
