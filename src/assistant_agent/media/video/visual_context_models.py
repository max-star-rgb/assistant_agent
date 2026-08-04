"""Immutable, prompt-safe visual context state models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MAX_CONTEXT_ITEM_LENGTH = 1_000
_MAX_RECORD_ID_LENGTH = 160


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
    return [
        _normalized_required_string(item, max_length=max_length)
        for item in value
    ]


class VisualContextSummary(BaseModel):
    """One revisioned summary that covers a continuous record prefix."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["visual_context_summary_v1"] = "visual_context_summary_v1"
    video_id: str = Field(min_length=1, max_length=240)
    summary_revision: int = Field(ge=1)
    covered_record_ids: list[str] = Field(min_length=1)
    first_sequence: int = Field(ge=0)
    last_sequence: int = Field(ge=0)
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

    @field_validator("covered_record_ids", mode="before")
    @classmethod
    def normalize_covered_record_ids(cls, value: object) -> list[str]:
        return _normalized_string_list(value, max_length=_MAX_RECORD_ID_LENGTH)

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
        if len(set(self.covered_record_ids)) != len(self.covered_record_ids):
            raise ValueError("visual context covered record ids must be unique")
        if self.first_sequence > self.last_sequence:
            raise ValueError("visual context sequences must be ordered")
        if (
            self.first_captured_at_ms is not None
            and self.last_captured_at_ms is not None
            and self.first_captured_at_ms > self.last_captured_at_ms
        ):
            raise ValueError("visual context capture times must be ordered")
        return self


class VisualContextSnapshot(BaseModel):
    """The latest compacted context held independently from raw records."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    summary: VisualContextSummary | None = None

    @field_validator("video_id", mode="before")
    @classmethod
    def normalize_video_id(cls, value: object) -> str:
        return _normalized_required_string(value, max_length=240)
