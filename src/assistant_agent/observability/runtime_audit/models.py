"""Versioned contracts for the read-only runtime audit loop."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from assistant_agent.providers.provider_errors import (
    sanitize_error_detail,
    sanitize_error_message,
)


AuditCategory = Literal["coverage", "infrastructure", "quality", "memory", "tool"]
AuditSeverity = Literal["info", "warning", "error"]


class LangfuseObservationSnapshot(BaseModel):
    """One observation as returned by the Langfuse read API."""

    observation_id: str = Field(min_length=1)
    name: str = ""
    type: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None
    level: str | None = None
    status_message: str | None = None
    input: Any = None
    output: Any = None
    metadata: Any = None

    @classmethod
    def from_api_payload(cls, payload: Any) -> "LangfuseObservationSnapshot":
        value = _model_mapping(payload)
        value["observation_id"] = value.get("observation_id") or value.get("id")
        for key in ("input", "output", "metadata"):
            value[key] = sanitize_error_detail(value.get(key))
        if isinstance(value.get("status_message"), str):
            value["status_message"] = sanitize_error_message(value["status_message"])
        return cls.model_validate(value)


class LangfuseScoreSnapshot(BaseModel):
    """One normalized Langfuse Score without assuming a concrete data type."""

    score_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: Any = None
    observation_id: str | None = None
    source: str | None = None
    metadata: Any = None
    created_at: datetime | None = None

    @classmethod
    def from_api_payload(cls, payload: Any) -> "LangfuseScoreSnapshot":
        value = _model_mapping(payload)
        value["score_id"] = value.get("score_id") or value.get("id")
        subject = value.get("subject")
        if not value.get("observation_id") and subject is not None:
            subject_value = _model_mapping(subject)
            if subject_value.get("kind") == "observation":
                value["observation_id"] = subject_value.get("id")
        value["created_at"] = value.get("created_at") or value.get("timestamp")
        value["metadata"] = sanitize_error_detail(value.get("metadata"))
        return cls.model_validate(value)


class LangfuseTraceSnapshot(BaseModel):
    """Full Langfuse trace snapshot used as the normal audit evidence."""

    trace_id: str = Field(min_length=1)
    name: str | None = None
    timestamp: datetime
    session_id: str | None = None
    user_id: str | None = None
    environment: str | None = None
    input: Any = None
    output: Any = None
    metadata: Any = None
    tags: list[str] = Field(default_factory=list)
    observations: list[LangfuseObservationSnapshot] = Field(default_factory=list)
    scores: list[LangfuseScoreSnapshot] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def from_api_payload(cls, payload: Any) -> "LangfuseTraceSnapshot":
        value = _model_mapping(payload)
        value["trace_id"] = value.get("trace_id") or value.get("id")
        for key in ("input", "output", "metadata"):
            value[key] = sanitize_error_detail(value.get(key))
        value["observations"] = [
            LangfuseObservationSnapshot.from_api_payload(item)
            for item in value.get("observations") or []
        ]
        value["scores"] = [
            LangfuseScoreSnapshot.from_api_payload(item)
            for item in value.get("scores") or []
        ]
        return cls.model_validate(value)


class LocalTraceManifest(BaseModel):
    """Minimal local completeness sentinel; it is not the normal evidence source."""

    trace_id: str
    run_id: str
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = Field(ge=1)
    terminal_event: str | None = None


class LocalFallbackEvent(BaseModel):
    """Bounded, redacted event included only for an export gap."""

    canonical_event: str | None = None
    event_type: str
    node_name: str
    status: str | None = None
    tool_name: str | None = None
    error_code: str | None = None
    created_at: datetime


class LocalTraceFallback(BaseModel):
    """Local evidence attached only when Langfuse has no corresponding trace."""

    trace_id: str
    run_id: str
    event_count: int = Field(ge=1)
    terminal_event: str | None = None
    timeline: list[LocalFallbackEvent] = Field(default_factory=list)


class AuditFinding(BaseModel):
    """One deterministic audit fact or quality finding."""

    code: str
    category: AuditCategory
    severity: AuditSeverity
    summary: str
    trace_id: str | None = None
    observation_id: str | None = None
    score_name: str | None = None
    quality_failure: bool = False


class AuditCoverage(BaseModel):
    langfuse_source_available: bool = True
    langfuse_trace_count: int = Field(ge=0)
    local_trace_count: int = Field(ge=0)
    matched_trace_count: int = Field(ge=0)
    missing_export_count: int = Field(ge=0)
    local_source_available: bool


class RuntimeAuditBundle(BaseModel):
    """Read-only input artifact consumed by deterministic and Codex reporting."""

    schema_version: Literal[
        "assistant_agent_runtime_audit_bundle_v1",
        "assistant_agent_runtime_audit_bundle_v2",
    ] = "assistant_agent_runtime_audit_bundle_v2"
    audit_run_id: str
    collected_at: datetime
    window_start: datetime
    window_end: datetime
    coverage: AuditCoverage
    traces: list[LangfuseTraceSnapshot] = Field(default_factory=list)
    local_manifests: list[LocalTraceManifest] = Field(default_factory=list)
    local_fallbacks: list[LocalTraceFallback] = Field(default_factory=list)
    findings: list[AuditFinding] = Field(default_factory=list)
    tool_catalogs: dict[str, list[Any]] = Field(default_factory=dict)
    production_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_tool_catalog_references(self) -> "RuntimeAuditBundle":
        if self.schema_version == "assistant_agent_runtime_audit_bundle_v1":
            return self
        for catalog_id, catalog in self.tool_catalogs.items():
            if not re.fullmatch(r"[0-9a-f]{64}", catalog_id):
                raise ValueError("tool catalog reference must be a SHA-256 digest")
            payload = json.dumps(
                catalog,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if hashlib.sha256(payload).hexdigest() != catalog_id:
                raise ValueError("tool catalog reference does not match its content")
        for trace in self.traces:
            values = [trace.input, *(item.input for item in trace.observations)]
            for value in values:
                for catalog_ref in _tool_catalog_refs(value):
                    if not isinstance(catalog_ref, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", catalog_ref
                    ):
                        raise ValueError("tool catalog reference must be a SHA-256 digest")
                    if catalog_ref not in self.tool_catalogs:
                        raise ValueError("tool catalog reference is not present in bundle")
        return self


def _tool_catalog_refs(value: Any):
    if isinstance(value, dict):
        if "tool_catalog_ref" in value:
            yield value["tool_catalog_ref"]
        for child in value.values():
            yield from _tool_catalog_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _tool_catalog_refs(child)


class AuditRecommendation(BaseModel):
    priority: Literal["low", "medium", "high"]
    area: Literal["runtime", "tool", "memory", "evaluation", "observability", "code"]
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    suggested_change: str
    validation: str


class CodexAuditReport(BaseModel):
    """Structured final response required from the isolated Codex process."""

    schema_version: Literal["assistant_agent_codex_audit_report_v1"] = (
        "assistant_agent_codex_audit_report_v1"
    )
    audit_run_id: str
    executive_summary: str
    coverage_assessment: str
    quality_findings: list[str] = Field(default_factory=list)
    memory_findings: list[str] = Field(default_factory=list)
    tool_trajectory_findings: list[str] = Field(default_factory=list)
    infrastructure_findings: list[str] = Field(default_factory=list)
    recommendations: list[AuditRecommendation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    production_mutation_allowed: Literal[False] = False


def _model_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="python"))
    raise TypeError(f"unsupported Langfuse payload type: {type(value).__name__}")
