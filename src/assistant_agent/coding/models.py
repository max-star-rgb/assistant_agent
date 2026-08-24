"""Stable contracts for governed coding workspaces and patch approval."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_CODING_ANALYSIS_READ_TOOL_NAMES = (
    "coding_repo_list",
    "coding_repo_search",
    "coding_repo_read",
    "coding_repo_status",
    "coding_repo_diff",
)
CODING_ANALYSIS_TASK_SPECS = MappingProxyType(
    {
        "structure_context": (
            "Identify relevant modules, interfaces, data flow, and existing "
            "implementation patterns in the frozen workspace snapshot.",
            _CODING_ANALYSIS_READ_TOOL_NAMES,
        ),
        "change_test_impact": (
            "Identify likely change surfaces, test entry points, compatibility "
            "constraints, and regression risks in the frozen workspace snapshot.",
            _CODING_ANALYSIS_READ_TOOL_NAMES,
        ),
        "safety_governance": (
            "Identify permission, credential, network, path, persistence, HITL, "
            "and governance boundaries in the frozen workspace snapshot.",
            _CODING_ANALYSIS_READ_TOOL_NAMES,
        ),
    }
)
CodingAnalysisSnapshotSchemaVersion = Literal[
    "legacy_v1",
    "immutable_manifest_v2",
]


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


class CodingAnalysisSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    materialization_schema_version: CodingAnalysisSnapshotSchemaVersion = "legacy_v1"
    snapshot_ref: str = Field(min_length=16, max_length=128)
    workspace_ref: str = Field(min_length=16, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _expiry_follows_creation(self) -> "CodingAnalysisSnapshot":
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("analysis snapshot timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("analysis snapshot expiry must follow creation")
        return self


class CodingAnalysisTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: Literal[
        "structure_context",
        "change_test_impact",
        "safety_governance",
    ]
    dimension: Literal[
        "structure_context",
        "change_test_impact",
        "safety_governance",
    ]
    objective: str = Field(min_length=1, max_length=2_000)
    allowed_tool_names: tuple[
        Literal[
            "coding_repo_list",
            "coding_repo_search",
            "coding_repo_read",
            "coding_repo_status",
            "coding_repo_diff",
        ],
        ...,
    ] = Field(min_length=1, max_length=5)

    @field_validator("allowed_tool_names", mode="before")
    @classmethod
    def _tuple_tool_names(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _task_matches_dimension(self) -> "CodingAnalysisTask":
        if self.task_id != self.dimension:
            raise ValueError("analysis task dimension must match task id")
        objective, allowed_tool_names = CODING_ANALYSIS_TASK_SPECS[self.task_id]
        if self.objective != objective:
            raise ValueError("analysis task objective must match the canonical task")
        if self.allowed_tool_names != allowed_tool_names:
            raise ValueError("analysis task tools must match the canonical task")
        return self


class CodingAnalysisFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    category: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    severity: Literal["info", "low", "moderate", "important"]
    summary: str = Field(min_length=1, max_length=2_000)
    path: str | None = Field(default=None, max_length=240)
    start_line: int | None = Field(default=None, ge=1, le=10_000_000)
    end_line: int | None = Field(default=None, ge=1, le=10_000_000)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = value.split("/")
        if (
            value.startswith("/")
            or any(part in {"", ".", "..", ".git"} for part in parts)
            or any(item in value for item in ("\\", "\x00", "\n", "\r"))
        ):
            raise ValueError("analysis finding path is invalid")
        return value

    @model_validator(mode="after")
    def _line_range_is_consistent(self) -> "CodingAnalysisFinding":
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("analysis finding line range must be supplied together")
        if self.start_line is not None:
            if self.path is None:
                raise ValueError("analysis finding line range requires a path")
            if self.end_line is not None and self.end_line < self.start_line:
                raise ValueError("analysis finding line range is reversed")
        return self


class CodingAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: Literal[
        "structure_context",
        "change_test_impact",
        "safety_governance",
    ]
    snapshot_ref: str = Field(min_length=16, max_length=128)
    tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["succeeded", "failed", "stale"]
    findings: tuple[CodingAnalysisFinding, ...] = Field(max_length=12)
    covered_paths: tuple[str, ...] = Field(max_length=128)
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^coding_analysis_[a-z0-9_]+$",
    )

    @field_validator("findings", "covered_paths", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("covered_paths")
    @classmethod
    def _safe_covered_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            parts = path.split("/")
            if (
                not path
                or len(path) > 240
                or path.startswith("/")
                or any(part in {"", ".", "..", ".git"} for part in parts)
                or any(item in path for item in ("\\", "\x00", "\n", "\r"))
            ):
                raise ValueError("analysis covered path is invalid")
        return value

    @model_validator(mode="after")
    def _status_matches_payload(self) -> "CodingAnalysisResult":
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful analysis result cannot carry an error")
        if self.status != "succeeded":
            if self.error_code is None:
                raise ValueError("unsuccessful analysis result requires an error code")
            if self.findings:
                raise ValueError("unsuccessful analysis result cannot carry findings")
        return self


CODING_REVIEW_TASK_SPECS = MappingProxyType(
    {
        "correctness_regression": "Review the proposed change for correctness, data flow, boundary handling, and regressions.",
        "security_governance": "Review the proposed change for security, permissions, trust boundaries, and governance.",
        "tests_validation": "Review tests, validation evidence, and residual regression risk.",
    }
)
LEGACY_CODING_REVIEW_TASK_SPECS = MappingProxyType(
    {
        "correctness": "Review the proposed change for correctness, data flow, and boundary handling.",
        "security": "Review the proposed change for security, permission, and governance regressions.",
        "regression": "Review the proposed change for compatibility, tests, and regression risk.",
    }
)
MAX_REVIEW_FINDINGS_PER_TASK = 8
MAX_REVIEW_EVIDENCE_PER_FINDING = 2


class CodingReviewTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    dimension: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    objective: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def _matches_canonical_task(self) -> "CodingReviewTask":
        task_specs = {**LEGACY_CODING_REVIEW_TASK_SPECS, **CODING_REVIEW_TASK_SPECS}
        if self.task_id not in task_specs:
            raise ValueError("review task is unknown")
        if self.dimension != self.task_id:
            raise ValueError("review task dimension must match task id")
        if self.objective != task_specs[self.task_id]:
            raise ValueError("review task objective must match the canonical task")
        return self


class CodingReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=240)
    line: int = Field(ge=1, le=10_000_000)
    excerpt: str = Field(min_length=1, max_length=800)
    content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        _validate_relative_policy_path(value, label="review evidence path")
        return value

    @model_validator(mode="after")
    def _digest_matches_evidence(self) -> "CodingReviewEvidence":
        payload = self.model_dump(mode="json", exclude={"evidence_digest"})
        expected_digests = {_canonical_digest(payload)}
        if self.content_digest is None:
            payload.pop("content_digest")
            expected_digests.add(_canonical_digest(payload))
        if self.evidence_digest not in expected_digests:
            raise ValueError("review evidence digest is invalid")
        return self


class CodingReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    severity: Literal["critical", "high", "medium", "moderate", "low", "info"]
    category: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    title: str | None = Field(default=None, min_length=1, max_length=240)
    explanation: str | None = Field(default=None, min_length=1, max_length=1_200)
    remediation: str | None = Field(default=None, min_length=1, max_length=1_200)
    summary: str | None = Field(default=None, min_length=1, max_length=1_200)
    evidence: tuple[CodingReviewEvidence, ...] = Field(
        min_length=1,
        max_length=MAX_REVIEW_EVIDENCE_PER_FINDING,
    )
    semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("evidence", mode="before")
    @classmethod
    def _tuple_evidence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _digests_match_finding(self) -> "CodingReviewFinding":
        current_fields = (self.title, self.explanation, self.remediation)
        if any(item is not None for item in current_fields):
            if any(item is None for item in current_fields) or self.summary is not None:
                raise ValueError("current review findings require title, explanation, and remediation only")
            if self.severity not in {"critical", "high", "medium", "low"}:
                raise ValueError("current review finding severity is invalid")
            if any(item.content_digest is None for item in self.evidence):
                raise ValueError("current review evidence requires a content digest")
            semantic_payload = {
                "severity": self.severity,
                "category": self.category,
                "title": self.title,
                "explanation": self.explanation,
                "remediation": self.remediation,
            }
        else:
            if self.summary is None:
                raise ValueError("legacy review findings require a summary")
            semantic_payload = {
                "severity": self.severity,
                "category": self.category,
                "summary": self.summary,
            }
        if self.semantic_key != _canonical_digest(semantic_payload):
            raise ValueError("review finding semantic key is invalid")
        evidence_payload = [
            item.model_dump(mode="json", exclude={"evidence_digest"})
            for item in self.evidence
        ]
        if self.title is None:
            for item in evidence_payload:
                if item.get("content_digest") is None:
                    item.pop("content_digest")
        expected_finding_id = _canonical_digest(
            {"task_id": self.task_id, **semantic_payload, "evidence": evidence_payload}
        )
        if self.finding_id != expected_finding_id:
            raise ValueError("review finding digest is invalid")
        return self


class CodingReviewerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    workspace_ref: str = Field(min_length=16, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_materialization_schema_version: CodingAnalysisSnapshotSchemaVersion = (
        "legacy_v1"
    )
    snapshot_created_at: datetime
    snapshot_expires_at: datetime
    generation: int | None = Field(default=None, ge=1)
    snapshot_ref: str | None = Field(default=None, min_length=16, max_length=256)
    tree_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_evidence_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    review_tasks: tuple[str, ...] = ()
    status: Literal["completed", "unavailable", "succeeded", "failed", "stale"]
    findings: tuple[CodingReviewFinding, ...] = Field(max_length=MAX_REVIEW_FINDINGS_PER_TASK)
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^coding_review_[a-z0-9_]+$",
    )

    @field_validator("findings", "review_tasks", mode="before")
    @classmethod
    def _tuple_findings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _status_and_digest_match_payload(self) -> "CodingReviewerResult":
        _validate_review_snapshot_timestamps(
            self.snapshot_created_at,
            self.snapshot_expires_at,
        )
        current = self.validation_evidence_digest is not None or bool(self.review_tasks)
        if current:
            if None in (self.generation, self.snapshot_ref, self.tree_digest):
                raise ValueError("current review result is missing snapshot binding")
            if self.review_tasks != tuple(CODING_REVIEW_TASK_SPECS):
                raise ValueError("current review result task inventory is invalid")
            if self.status not in {"completed", "unavailable"}:
                raise ValueError("current review result status is invalid")
        if self.status in {"completed", "succeeded"} and self.error_code is not None:
            raise ValueError("completed review result cannot carry an error")
        if self.status not in {"completed", "succeeded"}:
            if self.error_code is None:
                raise ValueError("unsuccessful review result requires an error code")
            if self.findings:
                raise ValueError("unsuccessful review result cannot carry findings")
        payload = self.model_dump(mode="json", exclude={"output_digest"})
        expected_digests = {_canonical_digest(payload)}
        if not current:
            for field in (
                "generation",
                "snapshot_ref",
                "tree_digest",
                "validation_evidence_digest",
                "review_tasks",
            ):
                payload.pop(field)
            expected_digests.add(_canonical_digest(payload))
        if self.snapshot_materialization_schema_version == "legacy_v1":
            payload.pop("snapshot_materialization_schema_version")
            expected_digests.add(_canonical_digest(payload))
        if self.output_digest not in expected_digests:
            raise ValueError("review result digest is invalid")
        return self


class CodingReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_ref: str = Field(min_length=16, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_materialization_schema_version: CodingAnalysisSnapshotSchemaVersion = (
        "legacy_v1"
    )
    snapshot_created_at: datetime
    snapshot_expires_at: datetime
    generation: int | None = Field(default=None, ge=1)
    snapshot_ref: str | None = Field(default=None, min_length=16, max_length=256)
    tree_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_evidence_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    review_tasks: tuple[str, ...] = ()

    @field_validator("review_tasks", mode="before")
    @classmethod
    def _tuple_review_tasks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _valid_snapshot_timestamps(self) -> "CodingReviewInput":
        _validate_review_snapshot_timestamps(
            self.snapshot_created_at,
            self.snapshot_expires_at,
        )
        if self.validation_evidence_digest is not None:
            if None in (self.generation, self.snapshot_ref, self.tree_digest):
                raise ValueError("current review input is missing snapshot binding")
            if self.review_tasks != tuple(CODING_REVIEW_TASK_SPECS):
                raise ValueError("current review input task inventory is invalid")
        return self


class CodingReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["clean", "findings", "unavailable"]
    workspace_ref: str = Field(min_length=16, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_materialization_schema_version: CodingAnalysisSnapshotSchemaVersion = (
        "legacy_v1"
    )
    snapshot_created_at: datetime
    snapshot_expires_at: datetime
    generation: int | None = Field(default=None, ge=1)
    snapshot_ref: str | None = Field(default=None, min_length=16, max_length=256)
    tree_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_evidence_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    review_tasks: tuple[str, ...] = ()
    results: tuple[CodingReviewerResult, ...] = Field(min_length=3, max_length=3)
    findings: tuple[CodingReviewFinding, ...] = Field(
        max_length=MAX_REVIEW_FINDINGS_PER_TASK * 3,
    )
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("results", "findings", "review_tasks", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _valid_snapshot_timestamps(self) -> "CodingReviewReport":
        _validate_review_snapshot_timestamps(
            self.snapshot_created_at,
            self.snapshot_expires_at,
        )
        if self.validation_evidence_digest is not None:
            if None in (self.generation, self.snapshot_ref, self.tree_digest):
                raise ValueError("current review report is missing snapshot binding")
            if self.review_tasks != tuple(CODING_REVIEW_TASK_SPECS):
                raise ValueError("current review report task inventory is invalid")
        return self


def _validate_review_snapshot_timestamps(
    created_at: datetime,
    expires_at: datetime,
) -> None:
    if created_at.utcoffset() is None or expires_at.utcoffset() is None:
        raise ValueError("review snapshot timestamps must be timezone-aware")
    if expires_at <= created_at:
        raise ValueError("review snapshot expiry must follow creation")


def _validate_relative_policy_path(value: str, *, label: str) -> None:
    parts = value.split("/")
    if (
        value.startswith("/")
        or any(part in {"", ".", "..", ".git"} for part in parts)
        or any(item in value for item in ("\\", "\x00", "\n", "\r"))
    ):
        raise ValueError(f"{label} is invalid")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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


class CodingLockedDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
    version: str = Field(min_length=1, max_length=128)
    sha256: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("sha256", mode="before")
    @classmethod
    def _tuple_hashes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingDependencyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    ecosystem: Literal["python-pip-wheel"]
    lockfile_path: str = Field(min_length=1, max_length=240)
    lockfile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    packages: tuple[CodingLockedDependency, ...] = Field(min_length=1, max_length=4_096)
    package_count: int = Field(ge=1, le=4_096)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_ports: tuple[int, ...] = Field(min_length=1, max_length=8)
    timeout_seconds: int = Field(ge=10, le=1_800)
    max_download_bytes: int = Field(ge=1_048_576, le=4_294_967_296)
    max_files: int = Field(ge=1, le=4_096)
    max_file_bytes: int = Field(ge=1_048_576, le=1_073_741_824)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("packages", "allowed_hosts", "allowed_ports", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _count_matches_packages(self) -> "CodingDependencyPlan":
        if self.package_count != len(self.packages):
            raise ValueError("dependency package count mismatch")
        return self


class CodingDependencyApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject"]
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _approval_requires_digest(self) -> "CodingDependencyApprovalDecision":
        if self.decision == "approve" and self.plan_digest is None:
            raise ValueError("dependency approval requires plan digest")
        return self


class CodingCredentialRequest(BaseModel):
    """Checkpoint-safe request for a bounded private registry lease."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    credential_profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    dependency_profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    registry_host: str = Field(min_length=3, max_length=253)
    registry_base_path: str = Field(min_length=1, max_length=240)
    lease_ttl_seconds: int = Field(ge=30, le=900)
    dependency_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodingCredentialApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject"]
    request_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _approval_requires_digest(self) -> "CodingCredentialApprovalDecision":
        if self.decision == "approve" and self.request_digest is None:
            raise ValueError("credential approval requires request digest")
        return self


class CodingArtifactDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    url: str = Field(min_length=10, max_length=2_048)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=192)
    size_bytes: int = Field(ge=1, le=1_073_741_824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodingArtifactIngressPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    manifest_path: str = Field(min_length=1, max_length=240)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[CodingArtifactDescriptor, ...] = Field(min_length=1, max_length=512)
    artifact_count: int = Field(ge=1, le=512)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_ports: tuple[int, ...] = (443,)
    timeout_seconds: int = Field(ge=10, le=1_800)
    max_total_bytes: int = Field(ge=1_048_576, le=4_294_967_296)
    max_file_bytes: int = Field(ge=1_024, le=1_073_741_824)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifacts", "allowed_hosts", "allowed_ports", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _count_matches(self) -> "CodingArtifactIngressPlan":
        if self.artifact_count != len(self.artifacts):
            raise ValueError("artifact count mismatch")
        return self


class CodingArtifactApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject"]
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _approval_requires_digest(self) -> "CodingArtifactApprovalDecision":
        if self.decision == "approve" and self.plan_digest is None:
            raise ValueError("artifact approval requires plan digest")
        return self


class CodingScannedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=192)
    size_bytes: int = Field(ge=1, le=1_073_741_824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_status: Literal["clean"] = "clean"


class CodingArtifactIngressManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scanner_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[CodingScannedArtifact, ...] = Field(min_length=1, max_length=512)
    total_bytes: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifacts", mode="before")
    @classmethod
    def _tuple_artifacts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingDependencyWheel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    filename: str = Field(min_length=5, max_length=255)
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
    version: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class CodingDependencyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lockfile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    wheels: tuple[CodingDependencyWheel, ...] = Field(min_length=1, max_length=4_096)
    total_bytes: int = Field(ge=0)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_profile_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$"
    )
    credential_policy_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    credential_request_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    credential_lease_id_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    credential_lease_issued_at: datetime | None = None
    credential_lease_expires_at: datetime | None = None
    credential_acquire_status: Literal["acquired"] | None = None
    credential_inject_status: Literal["injected"] | None = None
    credential_cleanup_status: Literal["revoked"] | None = None
    credential_lease_status: Literal["used"] | None = None

    @field_validator("wheels", mode="before")
    @classmethod
    def _tuple_wheels(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _credential_evidence_is_atomic(self) -> "CodingDependencyManifest":
        values = (
            self.credential_profile_id,
            self.credential_policy_digest,
            self.credential_request_digest,
            self.credential_lease_id_digest,
            self.credential_lease_issued_at,
            self.credential_lease_expires_at,
            self.credential_acquire_status,
            self.credential_inject_status,
            self.credential_cleanup_status,
            self.credential_lease_status,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("credential lease evidence must be supplied together")
        return self


class CodingArtifactExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    export_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    path: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=3, max_length=192)
    max_bytes: int = Field(ge=1_024, le=1_073_741_824)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("/")
            or any(part in {"", ".", "..", ".git"} for part in parts)
            or any(item in value for item in ("\\", "\x00", "\n", "\r"))
        ):
            raise ValueError("artifact export path is invalid")
        return value


class CodingArtifactExportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    export_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    path: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=3, max_length=192)
    size_bytes: int = Field(ge=1, le=1_073_741_824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodingArtifactExportManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    command_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    artifacts: tuple[CodingArtifactExportRecord, ...] = Field(min_length=1)
    artifact_count: int = Field(ge=1, le=512)
    total_bytes: int = Field(ge=1, le=4_294_967_296)
    scanner_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_ref: str = Field(pattern=r"^artifact_bundle_[0-9a-f]{32}$")
    created_at: datetime
    expires_at: datetime

    @field_validator("artifacts", mode="before")
    @classmethod
    def _freeze_artifacts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _totals_match(self) -> "CodingArtifactExportManifest":
        if self.artifact_count != len(self.artifacts):
            raise ValueError("artifact export count mismatch")
        if self.total_bytes != sum(item.size_bytes for item in self.artifacts):
            raise ValueError("artifact export size mismatch")
        return self


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
    dependency_root: Path | None = None
    dependency_lockfile_path: str | None = Field(default=None, max_length=240)
    dependency_plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    dependency_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_root: Path | None = None
    artifact_plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_exports: tuple["CodingArtifactExportRequest", ...] = ()
    artifact_export_root: Path | None = None

    @model_validator(mode="after")
    def _dependency_fields_are_atomic(self) -> "CodingSandboxRequest":
        values = (
            self.dependency_root,
            self.dependency_lockfile_path,
            self.dependency_plan_digest,
            self.dependency_manifest_digest,
        )
        if any(item is not None for item in values) and not all(
            item is not None for item in values
        ):
            raise ValueError("sandbox dependency fields must be supplied together")
        if self.dependency_lockfile_path is not None:
            parts = self.dependency_lockfile_path.split("/")
            if (
                self.dependency_lockfile_path.startswith("/")
                or any(part in {"", ".", "..", ".git"} for part in parts)
                or any(
                    item in self.dependency_lockfile_path
                    for item in ("\\", "\x00", "\n", "\r")
                )
            ):
                raise ValueError("sandbox dependency lockfile path is invalid")
        return self

    @model_validator(mode="after")
    def _artifact_fields_are_atomic(self) -> "CodingSandboxRequest":
        values = (
            self.artifact_root,
            self.artifact_plan_digest,
            self.artifact_manifest_digest,
        )
        if any(item is not None for item in values) and not all(
            item is not None for item in values
        ):
            raise ValueError("sandbox artifact fields must be supplied together")
        if bool(self.artifact_exports) != (self.artifact_export_root is not None):
            raise ValueError("sandbox artifact export fields must be supplied together")
        if self.artifact_exports and self.kind != "build":
            raise ValueError("sandbox artifact exports require a build command")
        export_ids = [item.export_id for item in self.artifact_exports]
        export_paths = [item.path for item in self.artifact_exports]
        if len(export_ids) != len(set(export_ids)) or len(export_paths) != len(
            set(export_paths)
        ):
            raise ValueError("sandbox artifact exports must be unique")
        return self


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
    dependency_plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    dependency_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    dependency_install_status: Literal["passed", "failed"] | None = None
    dependency_install_error: str | None = None
    artifact_plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_ingress_status: Literal["passed", "failed"] | None = None
    artifact_exports: tuple["CodingArtifactExportRecord", ...] = ()


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
    dependency_plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    dependency_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    dependency_install_status: Literal["passed", "failed"] | None = None
    dependency_install_error: str | None = None
    credential_profile_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$"
    )
    credential_policy_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    credential_request_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    credential_lease_id_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    credential_lease_issued_at: datetime | None = None
    credential_lease_expires_at: datetime | None = None
    credential_acquire_status: Literal["not_attempted", "failed", "acquired"] | None = None
    credential_inject_status: Literal["not_attempted", "failed", "injected"] | None = None
    credential_cleanup_status: Literal["not_required", "failed", "revoked"] | None = None
    credential_lease_status: Literal["used", "failed"] | None = None
    artifact_plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_scanner_policy_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_ingress_status: Literal["passed", "failed"] | None = None
    artifact_export_manifest_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_export_bundle_ref: str | None = Field(
        default=None, pattern=r"^artifact_bundle_[0-9a-f]{32}$"
    )
    artifact_export_status: Literal["passed", "failed"] | None = None


class CodingVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed", "failed", "format_approval_required"]
    validated_snapshot: CodingAnalysisSnapshot | None = None
    validation_binding_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    evidence: tuple[CodingCommandEvidence, ...] = ()
    formatter_validation: CodingPatchValidation | None = None
    error_code: str | None = None


class CodingRepairFailureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: str
    kind: Literal["test", "lint", "build"]
    exit_code: int
    error_code: Literal["verification_command_failed"]
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: str = Field(max_length=16_777_216)
    stderr: str = Field(max_length=16_777_216)
    truncated: bool = False


class CodingRepairAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: int = Field(ge=1, le=2)
    failure_output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["passed", "failed"]


class CodingRepairApprovalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repair_round: int = Field(ge=1, le=2)
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cumulative_diff_preview: str = Field(max_length=32_000)


class CodingRepairApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["approve", "reject", "respond"]
    patch_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    candidate_diff_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    response: str | None = Field(default=None, max_length=4_000)


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
    validation_binding_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    review_report_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    reviewed_tree_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

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
    repair_status: Literal["passed", "exhausted", "no_progress"] | None = None
    repair_history: tuple[CodingRepairAttempt, ...] = ()
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

    @field_validator("changed_paths", "repair_history", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
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
    "CODING_ANALYSIS_TASK_SPECS",
    "CodingApprovalDecision",
    "CodingAnalysisFinding",
    "CodingAnalysisResult",
    "CodingAnalysisSnapshot",
    "CodingAnalysisTask",
    "CodingArtifactApprovalDecision",
    "CodingArtifactDescriptor",
    "CodingArtifactIngressPlan",
    "CodingArtifactIngressManifest",
    "CodingScannedArtifact",
    "CodingCommandEvidence",
    "CodingCommitResult",
    "CodingCredentialApprovalDecision",
    "CodingCredentialRequest",
    "CodingDependencyApprovalDecision",
    "CodingDependencyManifest",
    "CodingDependencyPlan",
    "CodingPatchApplyResult",
    "CodingPatchProposal",
    "CodingPatchValidation",
    "CodingRepairApprovalContext",
    "CodingRepairApprovalDecision",
    "CodingRepairAttempt",
    "CodingRepairFailureEvidence",
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
