"""Contracts for the offline, non-mutating Improvement Lab."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator


ImprovementTargetType = Literal["skill", "runtime", "code"]
ImprovementSourceType = Literal[
    "trajectory",
    "eval_failure",
    "test_failure",
    "metric_anomaly",
]
ImprovementSeverity = Literal["low", "medium", "high"]
ImprovementCheckStatus = Literal["passed", "failed", "not_run"]
ImprovementCandidateStatus = Literal[
    "proposed",
    "evaluation_failed",
    "ready_for_review",
    "rejected",
    "accepted",
]


class ImprovementTargetRef(BaseModel):
    """Prompt-safe reference to an improvement target."""

    target_type: ImprovementTargetType
    target_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_skill_target(self) -> "ImprovementTargetRef":
        _require_safe_skill_id(self.target_type, self.target_ref)
        return self


class ImprovementEvidence(BaseModel):
    """One immutable, prompt-safe fact consumed by improvement analysis."""

    schema_version: Literal["improvement_evidence_v1"] = "improvement_evidence_v1"
    evidence_id: str = Field(min_length=1)
    source_type: ImprovementSourceType
    source_ref: str = Field(min_length=1)
    occurred_at: datetime | None = None
    component: str | None = None
    target_hints: list[ImprovementTargetRef] = Field(default_factory=list)
    symptom_code: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1_200)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    severity: ImprovementSeverity = "medium"
    redacted: bool = True

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ImprovementOpportunity(BaseModel):
    """Deterministically grouped evidence eligible for proposal generation."""

    schema_version: Literal["improvement_opportunity_v1"] = "improvement_opportunity_v1"
    opportunity_id: str = Field(min_length=1)
    target_type: ImprovementTargetType
    target_ref: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    pattern_code: str = Field(min_length=1)
    problem_statement: str = Field(min_length=1)
    recurrence_count: int = Field(ge=1)
    source_type_count: int = Field(ge=1)
    impact: ImprovementSeverity
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_version: Literal["opportunity_confidence_v1"] = "opportunity_confidence_v1"
    status: Literal["insufficient_evidence", "ready_for_proposal"]
    blocked_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_skill_target(self) -> "ImprovementOpportunity":
        _require_safe_skill_id(self.target_type, self.target_ref)
        return self


class CandidateCheck(BaseModel):
    """One explicit local candidate gate result."""

    check_name: str = Field(min_length=1)
    status: ImprovementCheckStatus
    summary: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class CandidateEvaluation(BaseModel):
    """Independent local evaluation of one generated proposal."""

    schema_version: Literal["candidate_evaluation_v1"] = "candidate_evaluation_v1"
    checks: list[CandidateCheck] = Field(default_factory=list)
    regression_suites: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    ready_for_review: bool = False


class ImprovementCandidate(BaseModel):
    """Evidence-backed improvement proposal that never applies itself."""

    schema_version: Literal["improvement_candidate_v1"] = "improvement_candidate_v1"
    candidate_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    target_type: ImprovementTargetType
    target_ref: str = Field(min_length=1)
    current_version: str | None = None
    evidence_refs: list[str] = Field(min_length=1)
    failure_pattern: str = Field(min_length=1)
    root_cause_hypothesis: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    affected_locations: list[str] = Field(default_factory=list)
    expected_benefit: str = Field(min_length=1)
    patch_preview: str | None = None
    acceptance_criteria: list[str] = Field(min_length=1)
    suggested_test_suite_ids: list[str] = Field(default_factory=list)
    risk_level: ImprovementSeverity
    limitations: list[str] = Field(default_factory=list)
    evaluation: CandidateEvaluation = Field(default_factory=CandidateEvaluation)
    status: ImprovementCandidateStatus = "proposed"

    @model_validator(mode="after")
    def validate_patch_scope(self) -> "ImprovementCandidate":
        _require_safe_skill_id(self.target_type, self.target_ref)
        if self.target_type != "skill" and self.patch_preview is not None:
            raise ValueError("only skill candidates may contain patch_preview")
        return self


class CandidateEvaluationRecord(BaseModel):
    """Run-scoped immutable evaluation for a stable proposal candidate."""

    schema_version: Literal["candidate_evaluation_record_v1"] = "candidate_evaluation_record_v1"
    evaluation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evaluation: CandidateEvaluation


class ImprovementDecision(BaseModel):
    """Explicit human decision recorded separately from a candidate."""

    schema_version: Literal["improvement_decision_v1"] = "improvement_decision_v1"
    decision_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    decision: Literal["accepted", "rejected"]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reviewer: str = Field(min_length=1)
    notes: str = ""


class ImprovementRunIssue(BaseModel):
    """Prompt-safe issue encountered during one lab invocation."""

    code: str = Field(min_length=1)
    source_ref: str | None = None
    summary: str = Field(min_length=1)


class AllowlistedEvalResult(BaseModel):
    """Prompt-safe result from one fixed repository-owned validation suite."""

    schema_version: Literal["allowlisted_eval_result_v1"] = "allowlisted_eval_result_v1"
    validation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    status: Literal["passed", "failed", "error"]
    returncode: int | None = None
    summary: str = Field(min_length=1)


class ImprovementRunReport(BaseModel):
    """Complete prompt-safe result of one offline lab invocation."""

    schema_version: Literal["improvement_run_report_v1"] = "improvement_run_report_v1"
    run_id: str = Field(min_length=1)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    analysis_max_age_days: int = Field(default=30, ge=1)
    analysis_cutoff: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(days=30)
    )
    completed_at: datetime | None = None
    evidence: list[ImprovementEvidence] = Field(default_factory=list)
    opportunities: list[ImprovementOpportunity] = Field(default_factory=list)
    candidates: list[ImprovementCandidate] = Field(default_factory=list)
    issues: list[ImprovementRunIssue] = Field(default_factory=list)
    validation_results: list[AllowlistedEvalResult] = Field(default_factory=list)
    persisted: bool = False
    production_mutation_allowed: bool = False


def _require_safe_skill_id(target_type: ImprovementTargetType, target_ref: str) -> None:
    if target_type != "skill":
        return
    if target_ref in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target_ref):
        raise ValueError("skill target_ref must be a safe single path segment")
