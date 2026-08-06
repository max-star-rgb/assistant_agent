"""Daily runtime audit artifact models."""

from __future__ import annotations

from datetime import date
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from assistant_agent.observability.runtime_audit.safety import (
    sanitize_runtime_audit_text,
)


DailyAttemptStatus = Literal["running", "succeeded", "failed"]
IssueStatus = Literal[
    "open",
    "code_addressed",
    "runtime_verified",
    "regressed",
    "uncertain",
]
_SAFE_EVIDENCE_REF_SUFFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+=-]*$")
_EVIDENCE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:@+=-]*"
_TRACE_EVIDENCE_PATTERN = (
    rf"^trace:{_EVIDENCE_ID_PATTERN}"
    rf"(?:/(?:observation|score):{_EVIDENCE_ID_PATTERN})?$"
)
_TEST_PATH_SEGMENT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._@+=-]*"
_CODE_EVIDENCE_PATTERN = (
    r"^(?:code:[0-9A-Fa-f]{7,40}|"
    rf"test:tests/{_TEST_PATH_SEGMENT_PATTERN}"
    rf"(?:/{_TEST_PATH_SEGMENT_PATTERN})*)$"
)
TraceEvidenceRef = Annotated[
    str,
    StringConstraints(max_length=500, pattern=_TRACE_EVIDENCE_PATTERN),
]
CodeEvidenceRef = Annotated[
    str,
    StringConstraints(max_length=500, pattern=_CODE_EVIDENCE_PATTERN),
]
LimitedHumanText = Annotated[str, Field(max_length=2_000)]


class DailyAuditIssue(BaseModel):
    """A trace-backed problem tracked across daily runtime audits."""

    issue_key: str
    status: IssueStatus
    title: str
    plain_summary: str = ""
    user_impact: str = ""
    suggested_change: str = ""
    validation: str = ""
    first_seen: date
    last_seen: date
    trace_evidence_refs: list[str] = Field(default_factory=list)
    code_evidence_refs: list[str] = Field(default_factory=list)
    runtime_verification_refs: list[str] = Field(default_factory=list)

    @field_validator("issue_key")
    @classmethod
    def _normalize_issue_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("issue_key must not be blank")
        return normalized

    @field_validator("trace_evidence_refs", "runtime_verification_refs")
    @classmethod
    def _validate_trace_evidence_refs(cls, values: list[str]) -> list[str]:
        if any(not _is_prefixed_ref(value, "trace:") for value in values):
            raise ValueError("evidence reference must use trace:<nonempty-id>")
        return values

    @field_validator("code_evidence_refs")
    @classmethod
    def _validate_code_evidence_refs(cls, values: list[str]) -> list[str]:
        if any(
            not (_is_prefixed_ref(value, "code:") or _is_prefixed_ref(value, "test:"))
            for value in values
        ):
            raise ValueError(
                "evidence reference must use code:<nonempty-id> or test:<nonempty-id>"
            )
        return values


class CodexDailyAuditIssue(DailyAuditIssue):
    """Bounded Codex output view; persisted historical issues remain compatible."""

    issue_key: str = Field(max_length=200)
    title: str = Field(max_length=500)
    plain_summary: str = Field(default="", max_length=2_000)
    user_impact: str = Field(default="", max_length=2_000)
    suggested_change: str = Field(default="", max_length=2_000)
    validation: str = Field(default="", max_length=2_000)
    trace_evidence_refs: list[TraceEvidenceRef] = Field(
        default_factory=list, max_length=50
    )
    code_evidence_refs: list[CodeEvidenceRef] = Field(
        default_factory=list, max_length=50
    )
    runtime_verification_refs: list[TraceEvidenceRef] = Field(
        default_factory=list, max_length=50
    )

    @field_validator("issue_key")
    @classmethod
    def _validate_safe_issue_key(cls, value: str) -> str:
        if not _SAFE_EVIDENCE_REF_SUFFIX.fullmatch(value):
            raise ValueError("issue_key contains unsafe control characters")
        return value


class DailyCodexAuditReport(BaseModel):
    """Plain-language daily report returned by the isolated Codex process."""

    schema_version: Literal["assistant_agent_daily_codex_audit_v1"] = (
        "assistant_agent_daily_codex_audit_v1"
    )
    audit_date: date
    daily_summary: str = Field(max_length=2_000)
    activity_summary: str = Field(max_length=2_000)
    issues: list[CodexDailyAuditIssue] = Field(default_factory=list, max_length=50)
    memory_summary: str = Field(max_length=2_000)
    infrastructure_summary: str = Field(max_length=2_000)
    limitations: list[LimitedHumanText] = Field(default_factory=list, max_length=30)
    production_mutation_allowed: Literal[False] = False

    @field_validator("issues", mode="before")
    @classmethod
    def _normalize_issue_models(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [
            item.model_dump(mode="python")
            if isinstance(item, DailyAuditIssue)
            else item
            for item in value
        ]

    @field_validator(
        "daily_summary",
        "activity_summary",
        "memory_summary",
        "infrastructure_summary",
    )
    @classmethod
    def _normalize_human_summary(cls, value: str, info: ValidationInfo) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank")
        return normalized


class IssueRegistry(BaseModel):
    """Persisted lifecycle state for daily runtime-audit issues."""

    schema_version: Literal["assistant_agent_runtime_audit_issues_v1"] = (
        "assistant_agent_runtime_audit_issues_v1"
    )
    issues: dict[str, DailyAuditIssue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_issue_keys(self) -> "IssueRegistry":
        for issue_key, issue in self.issues.items():
            if issue_key != issue.issue_key:
                raise ValueError("issue registry key must match issue.issue_key")
        return self


def _is_prefixed_ref(value: str, prefix: str) -> bool:
    suffix = value[len(prefix) :]
    return value.startswith(prefix) and bool(suffix) and bool(
        _SAFE_EVIDENCE_REF_SUFFIX.fullmatch(suffix)
    )


class DailyAuditAttempt(BaseModel):
    schema_version: Literal["assistant_agent_daily_audit_attempt_v1"] = (
        "assistant_agent_daily_audit_attempt_v1"
    )
    attempt_id: str
    audit_date: date
    status: DailyAttemptStatus
    bundle_path: str
    codex_output_path: str | None = None
    error_summary: str | None = None

    @field_validator("error_summary")
    @classmethod
    def _sanitize_error_summary(cls, value: str | None) -> str | None:
        return None if value is None else sanitize_runtime_audit_text(value)


class DailyAuditWatermarkV2(BaseModel):
    schema_version: Literal["assistant_agent_runtime_audit_watermark_v2"] = (
        "assistant_agent_runtime_audit_watermark_v2"
    )
    last_completed_date: date
    last_attempt_id: str
    bundle_path: str


class _StrictDailyAuditIssue(DailyAuditIssue):
    model_config = ConfigDict(strict=True, extra="forbid")


class _StrictIssueRegistry(IssueRegistry):
    model_config = ConfigDict(strict=True, extra="forbid")

    issues: dict[str, _StrictDailyAuditIssue] = Field(default_factory=dict)


class _StrictDailyAuditAttempt(DailyAuditAttempt):
    model_config = ConfigDict(strict=True, extra="forbid")


class DailyAuditCommitIntent(BaseModel):
    """Strict journal contract for an idempotent multi-artifact daily commit."""

    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal["assistant_agent_daily_commit_intent_v2"]
    attempt: _StrictDailyAuditAttempt
    markdown: str
    registry: _StrictIssueRegistry | None
    commit_continuous_state: bool
    expected_predecessor_watermark: date | None
    previous_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_registry_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_commit_shape(self) -> "DailyAuditCommitIntent":
        if self.attempt.status != "running":
            raise ValueError("daily commit intent attempt must be running")
        if (self.registry is None) != (self.desired_registry_digest is None):
            raise ValueError("daily commit registry and desired digest must appear together")
        if not self.commit_continuous_state and self.registry is not None:
            raise ValueError("explicit daily refresh cannot carry continuous registry state")
        return self
