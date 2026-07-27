"""Prompt-safe Langfuse score writer for text turn diagnostics."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from inspect import Parameter, signature
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from assistant_agent.observability.otel_mapping import langfuse_trace_id
from assistant_agent.observability.langfuse_config import (
    langfuse_credentials_from_env,
    langfuse_host_from_env,
)
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.observability.trace_store import TraceEvent, redact_trace_event
from assistant_agent.observability.turn_evaluator import TurnDiagnostic, build_turn_diagnostic
from assistant_agent.observability.turn_summary import ASSISTANT_TURN_SUMMARY_EVENT


LANGFUSE_SCORE_SPEC_SCHEMA_VERSION = "assistant_agent_langfuse_score_v1"
DEFAULT_SCORE_QUEUE_CAPACITY = 1024
DEFAULT_SCORE_CLOSE_TIMEOUT_SECONDS = 1.0
DEFAULT_SCORE_HTTP_TIMEOUT_SECONDS = 5.0
ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED_ENV = "ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED"
ASSISTANT_AGENT_LANGFUSE_SCORE_URL_ENV = "ASSISTANT_AGENT_LANGFUSE_SCORE_URL"
ASSISTANT_AGENT_LANGFUSE_SCORE_BASE_URL_ENV = "ASSISTANT_AGENT_LANGFUSE_SCORE_BASE_URL"
ASSISTANT_AGENT_LANGFUSE_SCORE_TIMEOUT_ENV = "ASSISTANT_AGENT_LANGFUSE_SCORE_TIMEOUT"
ASSISTANT_AGENT_LANGFUSE_SCORE_QUEUE_CAPACITY_ENV = "ASSISTANT_AGENT_LANGFUSE_SCORE_QUEUE_CAPACITY"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_RAW_CONTENT_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:raw_)?(?:prompt|query|input|output|response|content|message)\b\s*[:=]\s*([^\s,;]+)"
)
_BASIC_AUTH_RE = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9._~+/=-]+")
_SECRET_TOKEN_RE = re.compile(r"(?i)\bsecret[-_][A-Za-z0-9._-]+\b")

ScoreDataType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN", "TEXT"]
ScoreValue = float | str | bool


class LangfuseScoreSpec(BaseModel):
    """Dependency-free score payload for Langfuse API/SDK ingestion."""

    schema_version: Literal["assistant_agent_langfuse_score_v1"] = (
        LANGFUSE_SCORE_SPEC_SCHEMA_VERSION
    )
    score_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    data_type: ScoreDataType
    value: ScoreValue
    trace_id: str | None = None
    observation_id: str | None = None
    session_id: str | None = None
    comment: str | None = None

    def to_langfuse_payload(self) -> dict[str, Any]:
        """Return the public Langfuse score API payload."""

        payload: dict[str, Any] = {
            "id": self.score_id,
            "name": self.name,
            "value": _langfuse_score_value(self.value, self.data_type),
            "dataType": self.data_type,
        }
        if self.trace_id:
            payload["traceId"] = self.trace_id
        if self.observation_id:
            payload["observationId"] = self.observation_id
        if self.session_id and not self.trace_id:
            payload["sessionId"] = self.session_id
        if self.comment:
            payload["comment"] = self.comment
        return payload


def _langfuse_score_value(value: ScoreValue, data_type: ScoreDataType) -> float | str:
    if data_type == "BOOLEAN":
        return 1.0 if bool(value) else 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return value


@dataclass(frozen=True)
class LangfuseScoreWriterConfig:
    """Environment-backed configuration for optional Langfuse score writes."""

    enabled: bool = False
    scores_url: str | None = None
    public_key: str | None = None
    secret_key: str | None = None
    timeout_seconds: float = DEFAULT_SCORE_HTTP_TIMEOUT_SECONDS
    queue_capacity: int = DEFAULT_SCORE_QUEUE_CAPACITY

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LangfuseScoreWriterConfig":
        values = os.environ if env is None else env
        public_key, secret_key = langfuse_credentials_from_env(values)
        return cls(
            enabled=_truthy_env_value(values.get(ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED_ENV)),
            scores_url=_scores_url_from_env(values),
            public_key=public_key,
            secret_key=secret_key,
            timeout_seconds=_optional_positive_float(
                values.get(ASSISTANT_AGENT_LANGFUSE_SCORE_TIMEOUT_ENV)
            )
            or DEFAULT_SCORE_HTTP_TIMEOUT_SECONDS,
            queue_capacity=_optional_positive_int(
                values.get(ASSISTANT_AGENT_LANGFUSE_SCORE_QUEUE_CAPACITY_ENV)
            )
            or DEFAULT_SCORE_QUEUE_CAPACITY,
        )


@dataclass(frozen=True)
class LangfuseScoreWriterSetup:
    """Result of constructing an optional Langfuse score writer."""

    status: Literal["disabled", "ready", "unavailable"]
    writer: "LangfuseScoreHttpWriter | None" = None
    reason: str | None = None


class LangfuseScoreWriter(Protocol):
    """Dependency-free score writer boundary."""

    def write_scores(self, scores: Sequence[LangfuseScoreSpec]) -> None:
        """Write one score batch."""


ScoreTransport = Callable[[Request, float], Any]


@dataclass
class _ScoreCommand:
    kind: str
    payload: Any = None
    done: Event | None = None
    result: dict[str, Any] = field(default_factory=dict)


def build_langfuse_score_specs(
    events: Iterable[TraceEvent | Mapping[str, Any]],
    *,
    payload: Mapping[str, Any] | None = None,
    diagnostic: TurnDiagnostic | None = None,
) -> list[LangfuseScoreSpec]:
    """Build prompt-safe Langfuse score specs from structured turn diagnostics."""

    safe_events = [_event_mapping(event) for event in events]
    trace_id = _trace_id(safe_events, payload or {})
    if not trace_id:
        return []
    session_id = _session_id(safe_events, payload or {})
    resolved_diagnostic = diagnostic or build_turn_diagnostic(safe_events, payload=payload)
    if resolved_diagnostic.task_outcome == "unknown":
        return []
    comment = _diagnostic_comment(resolved_diagnostic)
    scores = [
        _score(
            trace_id=trace_id,
            session_id=session_id,
            name="assistant_agent.task_outcome",
            data_type="CATEGORICAL",
            value=resolved_diagnostic.task_outcome,
            comment=comment,
        )
    ]
    if resolved_diagnostic.prerequisites:
        scores.append(
            _score(
                trace_id=trace_id,
                session_id=session_id,
                name="assistant_agent.prerequisites_resolved",
                data_type="BOOLEAN",
                value=not resolved_diagnostic.unresolved_prerequisites,
                comment=comment,
            )
        )
    if resolved_diagnostic.clarification_too_late:
        scores.append(
            _score(
                trace_id=trace_id,
                session_id=session_id,
                name="assistant_agent.clarification_too_late",
                data_type="BOOLEAN",
                value=True,
                comment=comment,
            )
        )
    if resolved_diagnostic.unnecessary_tool_calls > 0:
        scores.append(
            _score(
                trace_id=trace_id,
                session_id=session_id,
                name="assistant_agent.unnecessary_tool_calls",
                data_type="NUMERIC",
                value=float(resolved_diagnostic.unnecessary_tool_calls),
                comment=comment,
            )
        )
    return scores


def create_langfuse_score_writer(
    config: LangfuseScoreWriterConfig | None = None,
    *,
    transport: ScoreTransport | None = None,
) -> LangfuseScoreWriterSetup:
    """Build an optional Langfuse score writer without side effects when disabled."""

    resolved_config = config or LangfuseScoreWriterConfig.from_env()
    if not resolved_config.enabled:
        return LangfuseScoreWriterSetup(status="disabled")
    if not resolved_config.scores_url:
        return LangfuseScoreWriterSetup(
            status="unavailable",
            reason="Langfuse score URL is required",
        )
    if not resolved_config.public_key or not resolved_config.secret_key:
        return LangfuseScoreWriterSetup(
            status="unavailable",
            reason="Langfuse score credentials are required",
        )
    return LangfuseScoreWriterSetup(
        status="ready",
        writer=LangfuseScoreHttpWriter(
            scores_url=resolved_config.scores_url,
            public_key=resolved_config.public_key,
            secret_key=resolved_config.secret_key,
            timeout_seconds=resolved_config.timeout_seconds,
            transport=transport,
        ),
    )


def create_langfuse_score_trace_observer_from_env() -> LangfuseScoreTraceObserver | None:
    """Create an enabled Langfuse score observer when env configuration is ready."""

    config = LangfuseScoreWriterConfig.from_env()
    setup = create_langfuse_score_writer(config)
    if setup.status != "ready" or setup.writer is None:
        return None
    return LangfuseScoreTraceObserver(
        BufferedLangfuseScoreWriter(setup.writer, capacity=config.queue_capacity),
        enabled=True,
    )


class LangfuseScoreHttpWriter:
    """Minimal REST writer for Langfuse score ingestion."""

    def __init__(
        self,
        *,
        scores_url: str,
        public_key: str,
        secret_key: str,
        timeout_seconds: float = DEFAULT_SCORE_HTTP_TIMEOUT_SECONDS,
        transport: ScoreTransport | None = None,
    ) -> None:
        self.scores_url = scores_url
        self.public_key = public_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _urlopen_transport

    def write_scores(self, scores: Sequence[LangfuseScoreSpec]) -> None:
        for score in scores:
            request = Request(
                self.scores_url,
                data=json.dumps(score.to_langfuse_payload()).encode("utf-8"),
                headers={
                    "Authorization": _basic_auth_header(self.public_key, self.secret_key),
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            response = self.transport(request, self.timeout_seconds)
            status = _response_status(response)
            if not 200 <= status < 300:
                raise RuntimeError(f"Langfuse score write failed with HTTP {status}")
            read = getattr(response, "read", None)
            if callable(read):
                read()


class BufferedLangfuseScoreWriter:
    """Send score batches through a bounded background queue."""

    def __init__(
        self,
        sink: LangfuseScoreWriter,
        *,
        capacity: int = DEFAULT_SCORE_QUEUE_CAPACITY,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.sink = sink
        self.capacity = capacity
        self._queue: Queue[_ScoreCommand] = Queue(maxsize=capacity)
        self._state_lock = Lock()
        self._close_lock = Lock()
        self._dropped_batch_count = 0
        self._error_count = 0
        self._last_error: str | None = None
        self._closing = False
        self._closed = False
        self._worker = Thread(
            target=self._run,
            name="assistant-agent-langfuse-score-writer",
            daemon=True,
        )
        self._worker.start()

    @property
    def dropped_batch_count(self) -> int:
        with self._state_lock:
            return self._dropped_batch_count

    @property
    def error_count(self) -> int:
        with self._state_lock:
            return self._error_count

    @property
    def last_error(self) -> str | None:
        with self._state_lock:
            return self._last_error

    def write_scores(self, scores: Sequence[LangfuseScoreSpec]) -> None:
        with self._state_lock:
            closing = self._closing or self._closed
        if closing:
            self._record_drop()
            return
        try:
            self._queue.put_nowait(_ScoreCommand("write", list(scores)))
        except Full:
            self._record_drop()

    def flush(self, *, timeout: float = DEFAULT_SCORE_CLOSE_TIMEOUT_SECONDS) -> bool:
        if timeout <= 0:
            return False
        with self._state_lock:
            if self._closed:
                return True
        command = _ScoreCommand("flush", done=Event())
        deadline = monotonic() + timeout
        if not self._enqueue_control(command, timeout=timeout):
            return False
        return command.done.wait(timeout=max(0.0, deadline - monotonic()))

    def close(self, *, timeout: float = DEFAULT_SCORE_CLOSE_TIMEOUT_SECONDS) -> bool:
        if timeout <= 0:
            return False
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return True
                self._closing = True
            deadline = monotonic() + timeout
            flushed = self.flush(timeout=max(0.0, deadline - monotonic()))
            stopped = self._enqueue_control(
                _ScoreCommand("stop"),
                timeout=max(0.0, deadline - monotonic()),
            )
            if stopped:
                self._worker.join(timeout=max(0.0, deadline - monotonic()))
            with self._state_lock:
                self._closed = not self._worker.is_alive()
            return flushed and self._closed

    def shutdown(self, *, timeout: float = DEFAULT_SCORE_CLOSE_TIMEOUT_SECONDS) -> bool:
        return self.close(timeout=timeout)

    def _run(self) -> None:
        while True:
            try:
                command = self._queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                if command.kind == "write":
                    self.sink.write_scores(command.payload)
                elif command.kind == "flush":
                    self._flush_sink()
                elif command.kind == "stop":
                    self._close_sink()
                    return
            except Exception as exc:  # noqa: BLE001 - background score writer boundary.
                safe_error = _sanitize_score_error(exc)
                command.result["error"] = safe_error
                self._record_error(safe_error)
            finally:
                if command.done is not None:
                    command.done.set()
                self._queue.task_done()

    def _enqueue_control(self, command: _ScoreCommand, *, timeout: float) -> bool:
        if timeout <= 0:
            return False
        try:
            self._queue.put(command, timeout=timeout)
        except Full:
            return False
        return True

    def _flush_sink(self) -> None:
        method = getattr(self.sink, "flush", None)
        if callable(method):
            method()

    def _close_sink(self) -> None:
        method = getattr(self.sink, "shutdown", None)
        if not callable(method):
            method = getattr(self.sink, "close", None)
        if callable(method):
            method()

    def _record_drop(self) -> None:
        with self._state_lock:
            self._dropped_batch_count += 1

    def _record_error(self, safe_error: str) -> None:
        with self._state_lock:
            self._error_count += 1
            self._last_error = safe_error


class LangfuseScoreTraceObserver:
    """Hook observer that writes Langfuse scores once per completed text turn."""

    def __init__(
        self,
        writer: LangfuseScoreWriter,
        *,
        enabled: bool = False,
        max_buffered_runs: int = 256,
        max_events_per_run: int = 1024,
        continue_on_error: bool = True,
    ) -> None:
        if max_buffered_runs <= 0:
            raise ValueError("max_buffered_runs must be positive")
        if max_events_per_run <= 0:
            raise ValueError("max_events_per_run must be positive")
        self.writer = writer
        self.enabled = enabled
        self.max_buffered_runs = max_buffered_runs
        self.max_events_per_run = max_events_per_run
        self.continue_on_error = continue_on_error
        self._lock = Lock()
        self._events_by_run: dict[str, list[TraceEvent]] = {}
        self._exported_run_ids: set[str] = set()
        self._dropped_run_ids: set[str] = set()
        self._errors: list[str] = []
        self._exported_run_count = 0
        self._dropped_run_count = 0
        self._dropped_event_count = 0

    @property
    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    @property
    def error_count(self) -> int:
        with self._lock:
            return len(self._errors)

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._errors[-1] if self._errors else None

    @property
    def exported_run_count(self) -> int:
        with self._lock:
            return self._exported_run_count

    def on_trace_event(self, event: TraceEvent) -> None:
        if not self.enabled:
            return
        safe_event = redact_trace_event(event)
        events_to_export = self._buffer_event(safe_event)
        if events_to_export is None:
            return
        self._write_scores(events_to_export)

    def flush(self, *, timeout: float = DEFAULT_SCORE_CLOSE_TIMEOUT_SECONDS) -> bool:
        return self._call_writer_lifecycle("flush", timeout=timeout)

    def close(self, *, timeout: float = DEFAULT_SCORE_CLOSE_TIMEOUT_SECONDS) -> bool:
        if hasattr(self.writer, "shutdown"):
            return self._call_writer_lifecycle("shutdown", timeout=timeout)
        return self._call_writer_lifecycle("close", timeout=timeout)

    def _buffer_event(self, event: TraceEvent) -> list[TraceEvent] | None:
        with self._lock:
            run_id = event.run_id
            if run_id in self._exported_run_ids or run_id in self._dropped_run_ids:
                return None
            if run_id not in self._events_by_run:
                self._ensure_run_capacity_locked()
                self._events_by_run[run_id] = []
            events = self._events_by_run[run_id]
            if len(events) >= self.max_events_per_run:
                events.pop(0)
                self._dropped_event_count += 1
            events.append(event)
            if event.canonical_event != ASSISTANT_TURN_SUMMARY_EVENT:
                return None
            events_to_export = list(events)
            del self._events_by_run[run_id]
            self._exported_run_ids.add(run_id)
            return events_to_export

    def _ensure_run_capacity_locked(self) -> None:
        while len(self._events_by_run) >= self.max_buffered_runs:
            dropped_run_id = next(iter(self._events_by_run))
            del self._events_by_run[dropped_run_id]
            self._dropped_run_ids.add(dropped_run_id)
            self._dropped_run_count += 1

    def _write_scores(self, events: list[TraceEvent]) -> None:
        try:
            scores = build_langfuse_score_specs(events)
            if not scores:
                return
            self.writer.write_scores(scores)
        except Exception as exc:
            self._record_error(exc)
            if not self.continue_on_error:
                raise
            return
        with self._lock:
            self._exported_run_count += 1

    def _call_writer_lifecycle(self, method_name: str, *, timeout: float) -> bool:
        method = getattr(self.writer, method_name, None)
        if not callable(method):
            return True
        try:
            result = method(timeout=timeout) if _accepts_timeout(method) else method()
        except Exception as exc:
            self._record_error(exc)
            if not self.continue_on_error:
                raise
            return False
        return result is not False

    def _record_error(self, exc: BaseException) -> None:
        safe_error = _sanitize_score_error(exc)
        with self._lock:
            self._errors.append(safe_error)


def _event_mapping(event: TraceEvent | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event, TraceEvent):
        return redact_trace_event(event).model_dump(mode="python")
    return dict(event)


def _accepts_timeout(method: Callable[..., object]) -> bool:
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in parameters or any(
        parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _trace_id(events: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]) -> str | None:
    value = _string_or_none(payload.get("trace_id"))
    if value:
        return value
    for event in events:
        value = _string_or_none(event.get("trace_id"))
        if value:
            return value
    return None


def _session_id(events: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]) -> str | None:
    value = _string_or_none(payload.get("session_id"))
    if value:
        return value
    for event in events:
        value = _string_or_none(event.get("session_id"))
        if value:
            return value
    return None


def _score(
    *,
    trace_id: str,
    session_id: str | None,
    name: str,
    data_type: ScoreDataType,
    value: ScoreValue,
    comment: str | None,
) -> LangfuseScoreSpec:
    target_trace_id = langfuse_trace_id(trace_id)
    return LangfuseScoreSpec(
        score_id=f"{target_trace_id}:{name}",
        trace_id=target_trace_id,
        session_id=session_id,
        name=name,
        data_type=data_type,
        value=value,
        comment=comment,
    )


def _diagnostic_comment(diagnostic: TurnDiagnostic) -> str:
    parts = [
        f"task_outcome={diagnostic.task_outcome}",
        f"execution_status={diagnostic.execution_status}",
    ]
    if diagnostic.unresolved_prerequisites:
        parts.append("unresolved_prerequisites=" + ",".join(diagnostic.unresolved_prerequisites))
    if diagnostic.diagnostic_flags:
        parts.append("flags=" + " | ".join(diagnostic.diagnostic_flags[:3]))
    return _bounded_safe_comment("; ".join(parts))


def _bounded_safe_comment(value: object, *, max_chars: int = 500) -> str:
    text = _sanitize_score_error(value)
    return text[:max_chars]


def _truthy_env_value(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY_ENV_VALUES


def _first_non_empty(values: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _scores_url_from_env(values: Mapping[str, str]) -> str | None:
    explicit = _first_non_empty(values, ASSISTANT_AGENT_LANGFUSE_SCORE_URL_ENV)
    if explicit is not None:
        return explicit
    base_url = _first_non_empty(values, ASSISTANT_AGENT_LANGFUSE_SCORE_BASE_URL_ENV)
    if base_url is None:
        base_url = langfuse_host_from_env(values)
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/api/public/scores"):
        return trimmed
    if trimmed.endswith("/api/public"):
        return f"{trimmed}/scores"
    return f"{trimmed}/api/public/scores"


def _optional_positive_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _optional_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _urlopen_transport(request: Request, timeout: float) -> Any:
    return urlopen(request, timeout=timeout)


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        code = getcode()
        if isinstance(code, int):
            return code
    return 200


def _sanitize_score_error(value: object) -> str:
    if isinstance(value, HTTPError):
        safe_error = f"HTTP {value.code}"
    else:
        safe_error = sanitize_error_message(value)
    safe_error = _BASIC_AUTH_RE.sub("Basic [redacted]", safe_error)
    safe_error = _SECRET_TOKEN_RE.sub("[redacted]", safe_error)
    return _RAW_CONTENT_ASSIGNMENT_RE.sub(_redact_content_assignment, safe_error)


def _redact_content_assignment(match: re.Match[str]) -> str:
    return match.group(0).split(match.group(1), 1)[0] + "[redacted]"
