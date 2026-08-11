"""Narrow HTTP proxy for Langfuse Remote Custom Experiment triggers."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


RELEASE_REVIEW_PATH = "/internal/evals/langfuse/release-review"
RUNTIME_REGRESSION_PATH = "/internal/evals/langfuse/runtime-regression"
RELEASE_REVIEW_SIGNING_SECRET_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_SIGNING_SECRET"
)
RUNTIME_REGRESSION_SIGNING_SECRET_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RUNTIME_REGRESSION_SIGNING_SECRET"
)


@dataclass(frozen=True, slots=True)
class WebhookProxyConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 80
    upstream_host: str = "host.docker.internal"
    upstream_port: int = 8089
    upstream_timeout_seconds: float = 35.0
    release_review_signing_secret: str | None = None
    runtime_regression_signing_secret: str | None = None
    now: Callable[[], float] = time.time


def create_server(config: WebhookProxyConfig) -> ThreadingHTTPServer:
    """Create the internal-only webhook proxy server."""

    class WebhookProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            signing_secret = {
                RELEASE_REVIEW_PATH: config.release_review_signing_secret,
                RUNTIME_REGRESSION_PATH: config.runtime_regression_signing_secret,
            }.get(self.path)
            if self.path not in {RELEASE_REVIEW_PATH, RUNTIME_REGRESSION_PATH}:
                self.send_error(404)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            signature = self.headers.get("x-langfuse-signature")
            if not signature and signing_secret:
                signature = _signature_header(
                    body=body,
                    secret=signing_secret,
                    timestamp=int(config.now()),
                )
            upstream = HTTPConnection(
                config.upstream_host,
                config.upstream_port,
                timeout=config.upstream_timeout_seconds,
            )
            try:
                upstream.request(
                    "POST",
                    self.path,
                    body=body,
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type",
                            "application/json",
                        ),
                        "x-langfuse-signature": signature or "",
                    },
                )
                response = upstream.getresponse()
                response_body = response.read()
                self.send_response(response.status)
                self.send_header(
                    "Content-Type",
                    response.getheader("Content-Type", "application/json"),
                )
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
            except (HTTPException, OSError):
                response_body = b'{"error":"assistant_server_unavailable"}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
            finally:
                upstream.close()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return ThreadingHTTPServer(
        (config.bind_host, config.bind_port),
        WebhookProxyHandler,
    )


def _signature_header(*, body: bytes, secret: str, timestamp: int) -> str:
    message = str(timestamp).encode("utf-8") + b"." + body
    signature = hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={signature}"


def main() -> None:
    server = create_server(
        WebhookProxyConfig(
            release_review_signing_secret=(
                os.environ.get(RELEASE_REVIEW_SIGNING_SECRET_ENV) or None
            ),
            runtime_regression_signing_secret=(
                os.environ.get(RUNTIME_REGRESSION_SIGNING_SECRET_ENV) or None
            ),
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
