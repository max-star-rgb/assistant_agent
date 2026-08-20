"""Stable contracts for governed coding workspaces and patch approval."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CodingWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_ref: str = Field(min_length=16, max_length=128)
    root: Path
    repo_id: str = Field(min_length=1, max_length=80)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    expires_at: datetime


class CodingWorkspaceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["coding_workspace_v1"] = "coding_workspace_v1"
    workspace_ref: str = Field(min_length=16, max_length=128)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    thread_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo_id: str = Field(min_length=1, max_length=80)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    created_at: datetime
    expires_at: datetime
    frozen: bool = False


class CodingPatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    patch: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=4_000)
    changed_paths: tuple[str, ...] = Field(min_length=1)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_file_digests: dict[str, str]
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("changed_paths", mode="before")
    @classmethod
    def _tuple_paths(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingPatchValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["valid"] = "valid"
    proposal: CodingPatchProposal
    diff_preview: str = Field(max_length=32_000)


class CodingApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject", "respond"]
    patch_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response: str | None = Field(default=None, max_length=4_000)


class CodingPatchApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["applied"] = "applied"
    workspace_ref: str
    base_commit: str
    patch_digest: str
    changed_paths: tuple[str, ...]
    diff_summary: str = Field(max_length=32_000)

    @field_validator("changed_paths", mode="before")
    @classmethod
    def _tuple_paths(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingTerminalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["applied", "rejected", "failed", "unconfigured"]
    workspace_ref: str | None = None
    base_commit: str | None = None
    patch_digest: str | None = None
    changed_paths: tuple[str, ...] = ()
    error_code: str | None = None

    @field_validator("changed_paths", mode="before")
    @classmethod
    def _tuple_paths(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingToolScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    repo_id: str = Field(min_length=1, max_length=80)


class CodingListEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    kind: Literal["file", "directory"]
    size_bytes: int | None = Field(default=None, ge=0)


class CodingListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entries: tuple[CodingListEntry, ...]
    next_cursor: int | None = Field(default=None, ge=0)


class CodingSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    line_number: int = Field(ge=1)
    line: str = Field(max_length=2_000)


class CodingSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    matches: tuple[CodingSearchMatch, ...]
    next_cursor: int | None = Field(default=None, ge=0)


class CodingReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    content: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    next_line: int | None = Field(default=None, ge=1)


class CodingStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entries: tuple[str, ...]


class CodingDiffResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    diff: str
    truncated: bool = False


__all__ = [
    "CodingApprovalDecision",
    "CodingPatchApplyResult",
    "CodingPatchProposal",
    "CodingPatchValidation",
    "CodingDiffResult",
    "CodingListEntry",
    "CodingListResult",
    "CodingReadResult",
    "CodingSearchMatch",
    "CodingSearchResult",
    "CodingStatusResult",
    "CodingTerminalResult",
    "CodingToolScope",
    "CodingWorkspace",
    "CodingWorkspaceMetadata",
]
