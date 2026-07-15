"""Small dependency-free HTTP transport for local framework sidecars."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from assistant_agent.memory.framework.base import FrameworkHttpRequest
from assistant_agent.memory.remote import MemoryServiceOperationError
from assistant_agent.services.provider_errors import sanitize_error_message


FrameworkTransport = Callable[[FrameworkHttpRequest], Any]


def urllib_framework_transport(base_url: str) -> FrameworkTransport:
    normalized = base_url.rstrip("/")

    def send(request: FrameworkHttpRequest) -> Any:
        url = normalized + request.path
        if request.query:
            url += "?" + urllib.parse.urlencode(request.query)
        data = None if request.body is None else json.dumps(request.body).encode("utf-8")
        headers = {"Content-Type": "application/json", **dict(request.headers or {})}
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers=headers, method=request.method),
                timeout=request.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except Exception as exc:
            raise MemoryServiceOperationError(
                request.path,
                f"memory framework request failed: {sanitize_error_message(str(exc))}",
            ) from exc
        return payload

    return send
