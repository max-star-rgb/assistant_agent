"""Small dependency-free HTTP transport for Mem0."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from assistant_agent.providers.provider_errors import sanitize_error_message


class Mem0OperationError(RuntimeError):
    """Recoverable Mem0 dependency failure."""

    def __init__(self, operation: str, message: str, *, http_status: int | None = None,
                 reason_code: str = "request_failed") -> None:
        super().__init__(sanitize_error_message(message))
        self.operation = operation
        self.recoverable = True
        self.http_status = http_status
        self.reason_code = reason_code


@dataclass(frozen=True)
class Mem0HttpRequest:
    method: str
    path: str
    body: Mapping[str, Any] | None = None
    query: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = 5.0


Mem0Transport = Callable[[Mem0HttpRequest], Any]


def urllib_mem0_transport(base_url: str) -> Mem0Transport:
    normalized = base_url.rstrip("/")

    def send(request: Mem0HttpRequest) -> Any:
        url = normalized + request.path
        if request.query:
            url += "?" + urllib.parse.urlencode(request.query)
        data = None if request.body is None else json.dumps(request.body).encode("utf-8")
        headers = {"Content-Type": "application/json", **dict(request.headers or {})}
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    url,
                    data=data,
                    headers=headers,
                    method=request.method,
                ),
                timeout=request.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            reason_code = "http_error"
            try:
                error_body = json.loads(exc.read(4096))
                # Only known protocol codes are safe; never log arbitrary response text.
                if isinstance(error_body, dict) and error_body.get("detail") == "snapshot_reference_required":
                    reason_code = "snapshot_reference_required"
            except Exception:
                pass
            finally:
                exc.close()
            raise Mem0OperationError(
                request.path, f"Mem0 HTTP {exc.code}: {reason_code}",
                http_status=exc.code, reason_code=reason_code,
            ) from exc
        except Exception as exc:
            raise Mem0OperationError(
                request.path,
                f"Mem0 request failed: {sanitize_error_message(str(exc))}",
            ) from exc
        return payload

    return send
