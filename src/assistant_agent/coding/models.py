"""Stable contracts for governed coding workspaces and patch approval."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class CodingSandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    image: str = Field(
        min_length=73,
        max_length=512,
        pattern=r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$",
    )
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    scratch_root: Path
    command_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    kind: Literal["test", "lint", "format", "build"]
    timeout_seconds: int = Field(ge=1, le=1_800)
    cpu_seconds: int = Field(ge=1, le=1_800)
    cpu_cores: float = Field(ge=0.1, le=16.0)
    memory_bytes: int = Field(ge=67_108_864, le=17_179_869_184)
    max_processes: int = Field(ge=4, le=512)
    max_output_bytes: int = Field(ge=1_024, le=16_777_216)
    max_file_bytes: int = Field(ge=1_024, le=10_485_760)
    max_disk_bytes: int = Field(ge=1_048_576, le=17_179_869_184)
    max_files: int = Field(ge=16, le=1_000_000)
    max_changed_files: int = Field(ge=1, le=256)
    max_patch_bytes: int = Field(ge=1_024, le=1_048_576)


class CodingSandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed", "failed", "timed_out", "resource_exceeded"]
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    oom_killed: bool = False
    error_code: str | None = None
    cleanup_status: Literal["not_created", "removed", "failed"]
    formatter_files: dict[str, str] = Field(default_factory=dict)
    formatter_deletions: tuple[str, ...] = ()
    formatter_modes: dict[str, int] = Field(default_factory=dict)


class CodingCommandEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: str = Field(min_length=1, max_length=80)
    kind: Literal["test", "lint", "format", "build"]
    status: Literal["passed", "failed", "timed_out", "resource_exceeded"]
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: str
    stderr: str
    truncated: bool = False
    error_code: str | None = None
    cleanup_status: Literal["not_created", "removed", "failed"] | None = None
    timed_out: bool = False
    oom_killed: bool = False


class CodingVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed", "failed", "format_approval_required"]
    evidence: tuple[CodingCommandEvidence, ...] = ()
    formatter_validation: CodingPatchValidation | None = None
    error_code: str | None = None


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


class CodingCommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["committed"] = "committed"
    workspace_ref: str = Field(min_length=16, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    parent_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    changed_paths: tuple[str, ...] = Field(min_length=1)
    verification_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("changed_paths", mode="before")
    @classmethod
    def _tuple_paths(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingMergePreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    expected_target_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    target_branch: str = Field(min_length=1, max_length=160)
    strategy: Literal["fast_forward", "merge_commit"]
    result_tree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    result_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    merge_preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodingMergeApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject"]
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    expected_target_head: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )
    merge_preview_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _approve_requires_binding(self) -> "CodingMergeApprovalDecision":
        if self.decision == "approve" and not all(
            (self.source_commit, self.expected_target_head, self.merge_preview_digest)
        ):
            raise ValueError("coding merge approval requires frozen preview facts")
        return self


class CodingMergeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["merged"] = "merged"
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    previous_target_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    result_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    result_tree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    target_branch: str = Field(min_length=1, max_length=160)
    strategy: Literal["fast_forward", "merge_commit"]
    merge_preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodingTerminalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["applied", "merged", "rejected", "failed", "unconfigured"]
    workspace_ref: str | None = None
    base_commit: str | None = None
    patch_digest: str | None = None
    changed_paths: tuple[str, ...] = ()
    error_code: str | None = None
    verification_status: Literal["passed", "failed"] | None = None
    verification_evidence: tuple[CodingCommandEvidence, ...] = ()
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    expected_target_head: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )
    result_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    merge_preview_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

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
    "CodingCommandEvidence",
    "CodingCommitResult",
    "CodingPatchApplyResult",
    "CodingPatchProposal",
    "CodingPatchValidation",
    "CodingDiffResult",
    "CodingListEntry",
    "CodingListResult",
    "CodingMergeApprovalDecision",
    "CodingMergePreview",
    "CodingMergeResult",
    "CodingReadResult",
    "CodingSearchMatch",
    "CodingSearchResult",
    "CodingSandboxRequest",
    "CodingSandboxResult",
    "CodingStatusResult",
    "CodingTerminalResult",
    "CodingToolScope",
    "CodingVerificationResult",
    "CodingWorkspace",
    "CodingWorkspaceMetadata",
]
