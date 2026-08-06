"""Daily runtime audit artifact models."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


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


class IssueRegistry(BaseModel):
    """Persisted lifecycle state for daily runtime-audit issues."""

    schema_version: Literal["assistant_agent_runtime_audit_issues_v1"] = (
        "assistant_agent_runtime_audit_issues_v1"
    )
    issues: dict[str, DailyAuditIssue] = Field(default_factory=dict)


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
