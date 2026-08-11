from __future__ import annotations

import http.client
import json
import threading

import deploy.langfuse_eval_webhook.webhook_proxy as proxy_module
from deploy.langfuse_eval_webhook.webhook_proxy import WebhookProxyConfig, create_server


def test_proxy_forwards_runtime_regression_with_its_own_signature(monkeypatch) -> None:
    forwarded: list[tuple[str, str, bytes, dict[str, str]]] = []

    class FakeResponse:
        status = 202

        def read(self):
            return b'{"status":"accepted"}'

        def getheader(self, name, default=None):
            return "application/json" if name == "Content-Type" else default

    class FakeUpstream:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("assistant", 8089, 35.0)

        def request(self, method, path, body, headers):
            forwarded.append((method, path, body, headers))

        def getresponse(self):
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(proxy_module, "HTTPConnection", FakeUpstream)
    server = create_server(
        WebhookProxyConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            upstream_host="assistant",
            release_review_signing_secret="release-secret",
            runtime_regression_signing_secret="runtime-secret",
            now=lambda: 1000,
        )
    )
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    body = json.dumps({"datasetName": "assistant-agent-runtime-regressions"}).encode()
    connection = http.client.HTTPConnection(*server.server_address)
    connection.request(
        "POST",
        "/internal/evals/langfuse/runtime-regression",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response.read()
    thread.join(timeout=2)
    server.server_close()

    assert response.status == 202
    assert forwarded[0][0:3] == (
        "POST",
        "/internal/evals/langfuse/runtime-regression",
        body,
    )
    assert forwarded[0][3]["x-langfuse-signature"].startswith("t=1000,v1=")
