"""Provider-neutral LLM streaming events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.assistant_decision import NativeToolCall


LLMEventType = Literal["token_delta", "tool_call_delta", "completed", "error"]


class LLMProviderError(BaseModel):
    """Prompt-safe provider error carried by an LLM event."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class LLMToolCallDelta(BaseModel):
    """Provider-neutral streamed tool-call delta."""

    index: int = Field(ge=0)
    id: str | None = None
    type: str | None = "function"
    name_delta: str | None = None
    arguments_delta: str | None = None


class LLMEvent(BaseModel):
    """Internal provider-boundary event for LLM streaming."""

    event_type: LLMEventType
    provider: str = Field(min_length=1)
    model: str | None = None
    text: str | None = None
    tool_call_delta: LLMToolCallDelta | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: LLMProviderError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _ToolCallAccumulator:
    index: int
    id: str | None = None
    type: str | None = "function"
    name: str = ""
    arguments: str = ""


class LLMEventAccumulator:
    """Accumulate LLMEvent records into terminal provider output pieces."""

    def __init__(self) -> None:
        self._provider: str | None = None
        self._model: str | None = None
        self._content_parts: list[str] = []
        self._tool_calls: dict[int, _ToolCallAccumulator] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self._error: LLMProviderError | None = None

    @property
    def provider(self) -> str | None:
        return self._provider

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def response_text(self) -> str:
        return "".join(self._content_parts)

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason

    @property
    def usage(self) -> dict[str, Any]:
        return dict(self._usage)

    @property
    def error(self) -> LLMProviderError | None:
        return self._error

    def apply(self, event: LLMEvent) -> None:
        """Apply one provider-neutral event to the accumulator."""

        self._provider = event.provider
        if event.model is not None:
            self._model = event.model

        if event.event_type == "token_delta":
            if event.text:
                self._content_parts.append(event.text)
            return

        if event.event_type == "tool_call_delta":
            if event.tool_call_delta is not None:
                self._apply_tool_call_delta(event.tool_call_delta)
            return

        if event.event_type == "completed":
            if event.finish_reason is not None:
                self._finish_reason = event.finish_reason
            if event.usage:
                self._usage = dict(event.usage)
            return

        if event.event_type == "error":
            self._error = event.error

    def finalize_tool_calls(self, *, provider_format: str = "llm_event") -> list[NativeToolCall]:
        """Return finalized native tool calls sorted by streamed index."""

        calls: list[NativeToolCall] = []
        for _, current in sorted(self._tool_calls.items()):
            if not current.name:
                continue
            raw = {
                "id": current.id,
                "type": current.type or "function",
                "function": {
                    "name": current.name,
                    "arguments": current.arguments,
                },
            }
            calls.append(
                NativeToolCall(
                    id=current.id,
                    name=current.name,
                    arguments=_parse_arguments(current.arguments),
                    provider_format=provider_format,
                    raw=raw,
                )
            )
        return calls

    def _apply_tool_call_delta(self, delta: LLMToolCallDelta) -> None:
        current = self._tool_calls.setdefault(delta.index, _ToolCallAccumulator(index=delta.index))
        if delta.id is not None:
            current.id = delta.id
        if delta.type is not None:
            current.type = delta.type
        if delta.name_delta:
            current.name = _merge_name_delta(current.name, delta.name_delta)
        if delta.arguments_delta:
            current.arguments += delta.arguments_delta


def _merge_name_delta(current: str, delta: str) -> str:
    if not current:
        return delta
    if delta == current:
        return current
    if delta.startswith(current):
        return delta
    return current + delta


def _parse_arguments(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"__native_tool_arguments_error__": "arguments JSON parsing failed"}
    if isinstance(parsed, dict):
        return parsed
    return {"__native_tool_arguments_error__": "arguments JSON was not an object"}
