"""Strict product facts and their one-way projection onto ``AgentEvent``.

LangGraph owns execution and streams facts from graph nodes through the native
``custom`` mode.  This module is deliberately the only place that constructs
and emits product-facing ``AgentEvent`` records from those facts.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import RLock
from typing import Annotated, Any, Callable, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assistant_agent.api.models import ApiError
from assistant_agent.runtime.event_sink import EventSink
from assistant_agent.runtime.events import AgentEvent


RUNTIME_PRODUCT_FACT_SCHEMA_VERSION = "runtime_product_fact_v1"
_PRODUCT_FACT_ID = ContextVar[str | None]("assistant_agent_product_fact_id", default=None)


class RuntimeProductFactValidationError(ValueError):
    """Raised when a custom stream payload claims this schema but is invalid."""


class RuntimeProductFactConflictError(RuntimeError):
    """Raised when one fact id is reused for a different occurrence payload."""


class _RuntimeProductFactBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["runtime_product_fact_v1"] = (
        RUNTIME_PRODUCT_FACT_SCHEMA_VERSION
    )
    fact_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    session_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunStartedProductFact(_RuntimeProductFactBase):
    kind: Literal["run_started"] = "run_started"
    user_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    trace_id: str = Field(min_length=1, max_length=256)


class TextDeltaProductFact(_RuntimeProductFactBase):
    kind: Literal["text_delta"] = "text_delta"
    text: str = Field(min_length=1, max_length=65_536)
    source: str = Field(min_length=1, max_length=128)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    finish_reason: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolStartedProductFact(_RuntimeProductFactBase):
    kind: Literal["tool_started"] = "tool_started"
    tool_name: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    step_id: str = Field(min_length=1, max_length=256)
    text: str | None = Field(default=None, max_length=4096)
    pre_tool_call: dict[str, Any] = Field(default_factory=dict)


class ToolTerminalProductFact(_RuntimeProductFactBase):
    kind: Literal["tool_terminal"] = "tool_terminal"
    tool_name: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    step_id: str = Field(min_length=1, max_length=256)
    success: bool
    latency_ms: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    post_tool_call: dict[str, Any] = Field(default_factory=dict)
    contract: dict[str, Any] | None = None
    output_ref: str | None = Field(default=None, max_length=2048)
    error: ApiError | None = None
    code: str | None = Field(default=None, max_length=256)
    recovery_action: str | None = Field(default=None, max_length=256)


class ProductProgressFact(_RuntimeProductFactBase):
    kind: Literal["product_progress"] = "product_progress"
    event_type: Literal[
        "agent_trace_decision",
        "agent_trace_observation",
        "agent_trace_final_answer",
        "progress_message",
    ]
    tool_name: str | None = Field(default=None, max_length=256)
    output_ref: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, max_length=65_536)
    error: str | dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class WaitingInputProductFact(_RuntimeProductFactBase):
    kind: Literal["waiting_input"] = "waiting_input"
    interrupt_kind: Literal["approval", "input"]
    prompt: str = Field(min_length=1, max_length=4096)
    action_ref: str = Field(min_length=1, max_length=256)
    interrupt_id: str | None = Field(default=None, max_length=256)


class RunFinalProductFact(_RuntimeProductFactBase):
    kind: Literal["run_final"] = "run_final"
    text: str = Field(max_length=262_144)


class RunFailedProductFact(_RuntimeProductFactBase):
    kind: Literal["run_failed"] = "run_failed"
    error: ApiError


class RunCancelledProductFact(_RuntimeProductFactBase):
    kind: Literal["run_cancelled"] = "run_cancelled"
    error: ApiError


RuntimeProductFact = Annotated[
    Union[
        RunStartedProductFact,
        TextDeltaProductFact,
        ToolStartedProductFact,
        ToolTerminalProductFact,
        ProductProgressFact,
        WaitingInputProductFact,
        RunFinalProductFact,
        RunFailedProductFact,
        RunCancelledProductFact,
    ],
    Field(discriminator="kind"),
]

_FACT_ADAPTER = TypeAdapter(RuntimeProductFact)


def new_runtime_product_fact_id(kind: str) -> str:
    """Create a bounded occurrence identity; callers must reuse it on replay."""

    normalized = "".join(char for char in kind if char.isalnum() or char in "_.-")
    return f"pf.{normalized[:32] or 'fact'}.{uuid4().hex}"


def validate_runtime_product_fact(value: object) -> RuntimeProductFact:
    """Validate one strict fact without accepting undeclared transport fields."""

    try:
        return _FACT_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise RuntimeProductFactValidationError("invalid runtime product fact") from exc


def emit_product_fact(
    writer: Callable[[Any], None] | None,
    fact: RuntimeProductFact,
) -> None:
    """Write one strict fact to LangGraph's native custom stream."""

    if writer is None:
        return
    validated = validate_runtime_product_fact(fact)
    writer(validated.model_dump(mode="python"))


def current_product_fact_id() -> str | None:
    """Expose the active projection identity for ownership guards and adapters."""

    return _PRODUCT_FACT_ID.get()


class ProductEventProjector:
    """Project strict facts exactly once without interpreting graph internals."""

    def __init__(
        self,
        *,
        event_sink: EventSink | None,
        dedupe_capacity: int = 4096,
    ) -> None:
        if isinstance(dedupe_capacity, bool) or not 1 <= dedupe_capacity <= 65_536:
            raise ValueError("dedupe_capacity must be between 1 and 65536")
        self._event_sink = event_sink
        self._dedupe_capacity = dedupe_capacity
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._in_flight: dict[str, str] = {}
        self._lock = RLock()

    def project_part(self, part: object) -> AgentEvent | None:
        """Parse only native ``custom`` parts; never infer from other modes/ns."""

        if getattr(part, "type", None) != "custom":
            return None
        data = getattr(part, "data", None)
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="python")
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != RUNTIME_PRODUCT_FACT_SCHEMA_VERSION:
            return None
        return self.project_fact(data)

    def project_fact(self, fact: RuntimeProductFact | object) -> AgentEvent | None:
        """Map and emit one occurrence, sharing bounded dedupe across all inputs."""

        validated = validate_runtime_product_fact(fact)
        digest = _fact_digest(validated)
        with self._lock:
            prior = self._seen.get(validated.fact_id)
            if prior is not None:
                if prior != digest:
                    raise RuntimeProductFactConflictError(
                        "runtime product fact_id was reused with different content"
                    )
                self._seen.move_to_end(validated.fact_id)
                return None
            in_flight = self._in_flight.get(validated.fact_id)
            if in_flight is not None:
                if in_flight != digest:
                    raise RuntimeProductFactConflictError(
                        "runtime product fact_id conflicts with an in-flight fact"
                    )
                return None
            self._in_flight[validated.fact_id] = digest

        event = _agent_event_from_fact(validated)
        try:
            if self._event_sink is not None:
                token = _PRODUCT_FACT_ID.set(validated.fact_id)
                try:
                    self._event_sink.emit(event)
                finally:
                    _PRODUCT_FACT_ID.reset(token)
        except BaseException:
            with self._lock:
                self._in_flight.pop(validated.fact_id, None)
            raise

        with self._lock:
            self._in_flight.pop(validated.fact_id, None)
            self._seen[validated.fact_id] = digest
            self._seen.move_to_end(validated.fact_id)
            while len(self._seen) > self._dedupe_capacity:
                self._seen.popitem(last=False)
        return event


def _agent_event_from_fact(fact: RuntimeProductFact) -> AgentEvent:
    common = {
        "session_id": fact.session_id,
        "run_id": fact.run_id,
        "created_at": fact.occurred_at,
    }
    if isinstance(fact, RunStartedProductFact):
        return AgentEvent(
            type="task_started",
            payload={
                "user_id": fact.user_id,
                "agent_id": fact.agent_id,
                "trace_id": fact.trace_id,
            },
            **common,
        )
    if isinstance(fact, TextDeltaProductFact):
        payload = dict(fact.payload)
        if fact.provider is not None:
            payload["provider"] = fact.provider
        if fact.model is not None:
            payload["model"] = fact.model
        if fact.finish_reason is not None:
            payload["finish_reason"] = fact.finish_reason
        payload["source"] = fact.source
        return AgentEvent(type="response_delta", text=fact.text, payload=payload, **common)
    if isinstance(fact, ToolStartedProductFact):
        return AgentEvent(
            type="tool_started",
            tool_name=fact.tool_name,
            text=fact.text,
            payload=_compact(
                {
                    "tool_call_id": fact.tool_call_id,
                    "step_id": fact.step_id,
                    "pre_tool_call": fact.pre_tool_call,
                    "message": fact.text,
                }
            ),
            **common,
        )
    if isinstance(fact, ToolTerminalProductFact):
        payload: dict[str, Any] = {
            "tool_call_id": fact.tool_call_id,
            "step_id": fact.step_id,
            "latency_ms": fact.latency_ms,
            "retry_count": fact.retry_count,
            "post_tool_call": fact.post_tool_call,
        }
        if fact.contract is not None:
            payload["contract"] = fact.contract
        if fact.code is not None:
            payload["code"] = fact.code
        if fact.recovery_action is not None:
            payload["recovery_action"] = fact.recovery_action
        return AgentEvent(
            type="tool_finished" if fact.success else "tool_failed",
            tool_name=fact.tool_name,
            output_ref=fact.output_ref if fact.success else None,
            error=fact.error.model_dump(mode="json") if fact.error is not None else None,
            payload=payload,
            **common,
        )
    if isinstance(fact, ProductProgressFact):
        return AgentEvent(
            type=fact.event_type,
            tool_name=fact.tool_name,
            output_ref=fact.output_ref,
            text=fact.text,
            error=fact.error,
            payload=fact.payload,
            **common,
        )
    if isinstance(fact, WaitingInputProductFact):
        return AgentEvent(
            type="progress_message",
            text=fact.prompt,
            payload=_compact(
                {
                    "status": "waiting_input",
                    "interrupt_kind": fact.interrupt_kind,
                    "action_ref": fact.action_ref,
                    "interrupt_id": fact.interrupt_id,
                }
            ),
            **common,
        )
    if isinstance(fact, RunFinalProductFact):
        return AgentEvent(type="final_response", text=fact.text, **common)
    if isinstance(fact, RunFailedProductFact):
        return AgentEvent(
            type="task_failed",
            error=fact.error.model_dump(mode="json"),
            **common,
        )
    if isinstance(fact, RunCancelledProductFact):
        return AgentEvent(
            type="task_cancelled",
            error=fact.error.model_dump(mode="json"),
            **common,
        )
    raise AssertionError("unreachable RuntimeProductFact variant")


def _fact_digest(fact: RuntimeProductFact) -> str:
    payload = fact.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


__all__ = [
    "ProductEventProjector",
    "ProductProgressFact",
    "RunCancelledProductFact",
    "RunFailedProductFact",
    "RunFinalProductFact",
    "RunStartedProductFact",
    "RuntimeProductFact",
    "RuntimeProductFactConflictError",
    "RuntimeProductFactValidationError",
    "TextDeltaProductFact",
    "ToolStartedProductFact",
    "ToolTerminalProductFact",
    "WaitingInputProductFact",
    "current_product_fact_id",
    "emit_product_fact",
    "new_runtime_product_fact_id",
    "validate_runtime_product_fact",
]
