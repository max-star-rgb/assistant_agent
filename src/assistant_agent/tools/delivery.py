"""Validated user-visible delivery declarations carried by ToolMessage artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


DELIVERY_ARTIFACT_KEY = "assistant_agent_delivery_v1"


class ToolDeliveryArtifact(BaseModel):
    """Bounded material a successful Tool requires the transport to deliver."""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(default="", max_length=16_000)
    output_refs: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_content(self) -> "ToolDeliveryArtifact":
        if not self.text.strip() and not self.output_refs:
            raise ValueError("tool delivery must not be empty")
        if any(
            not value
            or len(value) > 2_048
            or value != value.strip()
            or any(character.isspace() or ord(character) < 32 for character in value)
            for value in self.output_refs
        ):
            raise ValueError("tool delivery output_refs are invalid")
        return self


def with_tool_delivery(
    artifact: Mapping[str, Any],
    *,
    text: str = "",
    output_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a domain artifact carrying one validated delivery declaration."""

    delivery = ToolDeliveryArtifact(text=text, output_refs=list(output_refs))
    return {
        **artifact,
        DELIVERY_ARTIFACT_KEY: delivery.model_dump(mode="json"),
    }


def read_tool_delivery(artifact: Any) -> ToolDeliveryArtifact | None:
    """Read the common declaration from a local or MCP Tool artifact."""

    if not isinstance(artifact, Mapping):
        return None
    payload = artifact.get(DELIVERY_ARTIFACT_KEY)
    if payload is None:
        structured = artifact.get("structured_content")
        if isinstance(structured, Mapping):
            payload = structured.get(DELIVERY_ARTIFACT_KEY)
    try:
        return (
            ToolDeliveryArtifact.model_validate(payload)
            if payload is not None
            else None
        )
    except ValidationError:
        return None


def safe_http_url(value: Any) -> bool:
    """Return whether a URL is safe to embed in the media Markdown protocol."""

    if not isinstance(value, str) or value != value.strip():
        return False
    if any(character.isspace() or character in "<>[]()" for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except (UnicodeError, ValueError):
        return False


__all__ = [
    "DELIVERY_ARTIFACT_KEY",
    "ToolDeliveryArtifact",
    "read_tool_delivery",
    "safe_http_url",
    "with_tool_delivery",
]
