"""Small stdlib client for the HTTP JSON/SSE Agent API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field


class AgentClientEvent(BaseModel):
    """One decoded event from `/agent/run` SSE mode."""

    model_config = ConfigDict(extra="forbid")

    event: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class HttpAgentClientError(RuntimeError):
    """Structured HTTP error raised by the local product client."""

    def __init__(self, *, status_code: int, detail: Any) -> None:
        super().__init__(f"Agent HTTP request failed with status {status_code}")
        self.status_code = status_code
        self.detail = detail


class HttpAgentClient:
    """Blocking JSON/SSE client used by CLI and local protocol checks."""

    def __init__(self, *, server: str, timeout_s: float = 120.0) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.server = server.rstrip("/")
        self.timeout_s = timeout_s

    def run_stream(
        self,
        request: Mapping[str, Any],
    ) -> Iterator[AgentClientEvent]:
        http_request = self._json_request(
            "/agent/run",
            request,
            accept="text/event-stream",
        )
        try:
            with urlopen(http_request, timeout=self.timeout_s) as response:
                yield from parse_sse_response(response)
        except HTTPError as exc:
            raise _http_error(exc) from exc

    def run_json(self, request: Mapping[str, Any]) -> dict[str, Any]:
        http_request = self._json_request(
            "/agent/run",
            request,
            accept="application/json",
        )
        return self._read_json(http_request)

    def cancel(
        self,
        *,
        run_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        http_request = self._json_request(
            f"/agent/runs/{quote(run_id, safe='')}/cancel",
            {"user_id": user_id, "session_id": session_id},
            accept="application/json",
        )
        return self._read_json(http_request)

    def _read_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise _http_error(exc) from exc
        if not isinstance(payload, dict):
            raise ValueError("Agent HTTP response must be a JSON object")
        return payload

    def _json_request(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        accept: str,
    ) -> Request:
        return Request(
            f"{self.server}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": accept,
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )


def parse_sse_response(response: Any) -> Iterator[AgentClientEvent]:
    """Decode SSE lines without assuming network chunk boundaries."""

    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            event = _event_from_fields(event_name=event_name, data_lines=data_lines)
            if event is not None:
                yield event
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    event = _event_from_fields(event_name=event_name, data_lines=data_lines)
    if event is not None:
        yield event


def _event_from_fields(
    *,
    event_name: str | None,
    data_lines: list[str],
) -> AgentClientEvent | None:
    if not event_name and not data_lines:
        return None
    raw_data = "\n".join(data_lines) if data_lines else "{}"
    payload = json.loads(raw_data)
    if not isinstance(payload, dict):
        raise ValueError("SSE data must be a JSON object")
    return AgentClientEvent(event=event_name or "message", data=payload)


def _http_error(exc: HTTPError) -> HttpAgentClientError:
    raw = exc.read()
    try:
        detail = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = raw.decode("utf-8", errors="replace")
    return HttpAgentClientError(status_code=exc.code, detail=detail)

