"""Daily runtime audit artifact models."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DailyAttemptStatus = Literal["running", "succeeded", "failed"]
IssueStatus = Literal[
    "open",
    "code_addressed",
    "runtime_verified",
    "regressed",
    "uncertain",
]


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
    return value.startswith(prefix) and bool(value[len(prefix) :]) and not any(
        character.isspace() for character in value
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


class DailyAuditWatermarkV2(BaseModel):
    schema_version: Literal["assistant_agent_runtime_audit_watermark_v2"] = (
        "assistant_agent_runtime_audit_watermark_v2"
    )
    last_completed_date: date
    last_attempt_id: str
    bundle_path: str
