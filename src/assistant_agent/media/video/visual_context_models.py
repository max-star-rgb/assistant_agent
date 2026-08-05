"""Immutable, prompt-safe visual context state models."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MAX_CONTEXT_ITEM_LENGTH = 1_000
_MAX_RECORD_ID_LENGTH = 160
_COVERAGE_DIGEST_LENGTH = 64
_COVERAGE_DIGEST_DOMAIN = b"assistant_agent.visual_context.coverage.v2\0"


def _normalized_required_string(value: object, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError("visual context value must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("visual context value must be non-empty")
    if len(normalized) > max_length:
        raise ValueError("visual context value exceeds maximum length")
    return normalized


def _normalized_string_list(value: object, *, max_length: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("visual context values must be a list")
    return [_normalized_required_string(item, max_length=max_length) for item in value]


def _normalized_coverage_digest(value: object) -> str:
    normalized = _normalized_required_string(
        value,
        max_length=_COVERAGE_DIGEST_LENGTH,
    ).lower()
    if len(normalized) != _COVERAGE_DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("visual context coverage digest must be SHA-256 hex")
    return normalized


def extend_visual_context_coverage_digest(
    previous_digest: str | None,
    records: list[tuple[str, int, int]],
) -> str:
    """Extend the bounded digest for one code-selected coverage wave."""

    if not records:
        raise ValueError("visual context coverage records must be non-empty")
    digest = hashlib.sha256()
    digest.update(_COVERAGE_DIGEST_DOMAIN)
    if previous_digest is not None:
        digest.update(_normalized_coverage_digest(previous_digest).encode("ascii"))
    for record_id, frame_sequence, created_at_ms in records:
        normalized_id = _normalized_required_string(
            record_id,
            max_length=_MAX_RECORD_ID_LENGTH,
        )
        if (
            isinstance(frame_sequence, bool)
            or not isinstance(frame_sequence, int)
            or frame_sequence < 0
            or isinstance(created_at_ms, bool)
            or not isinstance(created_at_ms, int)
            or created_at_ms < 0
        ):
            raise ValueError("visual context coverage record metadata is invalid")
        digest.update(b"\0")
        digest.update(
            json.dumps(
                [normalized_id, frame_sequence, created_at_ms],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return digest.hexdigest()


class VisualContextSummary(BaseModel):
    """One revisioned summary that covers a continuous record prefix."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["visual_context_summary_v2"] = "visual_context_summary_v2"
    video_id: str = Field(min_length=1, max_length=240)
    summary_revision: int = Field(ge=1)
    covered_record_count: int = Field(ge=1)
    covered_through_sequence: int = Field(ge=0)
    coverage_digest: str = Field(min_length=64, max_length=64)
    first_sequence: int = Field(ge=0)
    first_captured_at_ms: int | None = Field(default=None, ge=0)
    last_captured_at_ms: int | None = Field(default=None, ge=0)
    stable_scene: list[str] = Field(default_factory=list)
    object_last_confirmed: list[str] = Field(default_factory=list)
    people_last_confirmed: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    source_token_count: int = Field(ge=0)
    summary_token_count: int = Field(ge=0)
    compactor_model: str = Field(default="", max_length=240)

    @field_validator("video_id", mode="before")
    @classmethod
    def normalize_video_id(cls, value: object) -> str:
        return _normalized_required_string(value, max_length=240)

    @field_validator("coverage_digest", mode="before")
    @classmethod
    def normalize_coverage_digest(cls, value: object) -> str:
        return _normalized_coverage_digest(value)

    @field_validator(
        "stable_scene",
        "object_last_confirmed",
        "people_last_confirmed",
        "changes",
        "uncertainties",
        mode="before",
    )
    @classmethod
    def normalize_context_items(cls, value: object) -> list[str]:
        return _normalized_string_list(value, max_length=_MAX_CONTEXT_ITEM_LENGTH)

    @field_validator("compactor_model", mode="before")
    @classmethod
    def normalize_compactor_model(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("visual context value must be a string")
        normalized = value.strip()
        if len(normalized) > 240:
            raise ValueError("visual context value exceeds maximum length")
        return normalized

    @model_validator(mode="after")
    def validate_coverage_range(self) -> "VisualContextSummary":
        if self.first_sequence > self.covered_through_sequence:
            raise ValueError("visual context sequences must be ordered")
        if (
            self.first_captured_at_ms is not None
            and self.last_captured_at_ms is not None
            and self.first_captured_at_ms > self.last_captured_at_ms
        ):
            raise ValueError("visual context capture times must be ordered")
        return self


def visual_context_summary_projection(
    summary: VisualContextSummary,
) -> dict[str, object]:
    """Return only bounded semantic/range facts useful to a model."""

    return {
        "summary_revision": summary.summary_revision,
        "first_sequence": summary.first_sequence,
        "covered_through_sequence": summary.covered_through_sequence,
        "first_captured_at_ms": summary.first_captured_at_ms,
        "last_captured_at_ms": summary.last_captured_at_ms,
        "stable_scene": summary.stable_scene,
        "object_last_confirmed": summary.object_last_confirmed,
        "people_last_confirmed": summary.people_last_confirmed,
        "changes": summary.changes,
        "uncertainties": summary.uncertainties,
    }


class VisualContextSnapshot(BaseModel):
    """The latest compacted context held independently from raw records."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    summary: VisualContextSummary | None = None

    @field_validator("video_id", mode="before")
    @classmethod
    def normalize_video_id(cls, value: object) -> str:
        return _normalized_required_string(value, max_length=240)
