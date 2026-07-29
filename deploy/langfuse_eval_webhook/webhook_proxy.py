"""Narrow HTTP proxy for Langfuse Remote Custom Experiment triggers."""

from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


REMOTE_EXPERIMENT_PATH = "/internal/evals/langfuse/remote-experiment"


@dataclass(frozen=True, slots=True)
class WebhookProxyConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 80
    upstream_host: str = "host.docker.internal"
    upstream_port: int = 8089
    upstream_timeout_seconds: float = 10.0


def create_server(config: WebhookProxyConfig) -> ThreadingHTTPServer:
    """Create the internal-only webhook proxy server."""

    class WebhookProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != REMOTE_EXPERIMENT_PATH:
                self.send_error(404)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            upstream = HTTPConnection(
                config.upstream_host,
                config.upstream_port,
                timeout=config.upstream_timeout_seconds,
            )
            try:
                upstream.request(
                    "POST",
                    REMOTE_EXPERIMENT_PATH,
                    body=body,
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type",
                            "application/json",
                        ),
                        "x-langfuse-signature": self.headers.get(
                            "x-langfuse-signature",
                            "",
                        ),
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


def main() -> None:
    server = create_server(WebhookProxyConfig())
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
