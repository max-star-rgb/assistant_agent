"""Strict assistant turn outputs for text delivery and tool governance."""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantTextOutput(BaseModel):
    """A non-empty text response selected for delivery."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)
    reason: str | None = Field(
        default=None,
        description="Brief runtime routing reason, not chain-of-thought.",
    )
    safety_notes: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace content")
        return value


class AssistantToolCall(BaseModel):
    """One model-proposed tool call awaiting deterministic validation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    tool_name: str = Field(min_length=1)
    tool_input: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = Field(default=None, description="Bound plan step id.")
    reason: str | None = Field(
        default=None,
        description="Brief high-level routing reason, not chain-of-thought.",
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    safety_notes: list[str] = Field(default_factory=list)

    @field_validator("tool_name")
    @classmethod
    def reject_blank_tool_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool_name must contain non-whitespace content")
        return value


AssistantTurnOutput = Annotated[
    AssistantTextOutput | AssistantToolCall,
    Field(discriminator="type"),
]


class NativeToolCall(BaseModel):
    """Provider-native tool call normalized at the adapter boundary."""

    id: str | None = None
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    provider_format: str = "openai_compatible"
    raw: dict[str, Any] = Field(default_factory=dict)

    def to_assistant_tool_call(self) -> AssistantToolCall:
        """Convert to the governed internal tool-call contract."""

        return AssistantToolCall(
            tool_name=self.name,
            tool_input=self.arguments,
            reason=f"Provider-native tool call ({self.provider_format}).",
            safety_notes=["native_tool_call"],
        )


def native_tool_call_to_assistant_output(call: NativeToolCall) -> AssistantToolCall:
    """Convert a normalized native tool call to a strict assistant output."""

    return call.to_assistant_tool_call()


def openai_tool_call_to_native_tool_call(payload: dict[str, Any]) -> NativeToolCall:
    """Parse an OpenAI-compatible tool call payload."""

    function = payload.get("function") if isinstance(payload.get("function"), dict) else {}
    name = function.get("name") or payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("native tool call missing function name")
    return NativeToolCall(
        id=str(payload.get("id")) if payload.get("id") is not None else None,
        name=name,
        arguments=_parse_native_arguments(
            function.get("arguments", payload.get("arguments"))
        ),
        provider_format="openai_compatible",
        raw=payload,
    )


def _parse_native_arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if value.strip() else {}
        except json.JSONDecodeError:
            return {"__native_tool_arguments_error__": "arguments JSON parsing failed"}
        if isinstance(parsed, dict):
            return parsed
        return {"__native_tool_arguments_error__": "arguments JSON was not an object"}
    return {"__native_tool_arguments_error__": "arguments were not a JSON object"}
