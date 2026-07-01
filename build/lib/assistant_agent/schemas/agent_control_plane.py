"""Stable read-only schemas for the local agent control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentControlPlaneRunRecord(BaseModel):
    """Redacted control-plane record for one run."""

    schema_version: str = "agent_control_plane_run_v1"
    run_id: str
    trace_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    status: str
    entrypoint: str = "agents_run"
    route_decision: dict[str, Any] = Field(default_factory=dict)
    delegated_tasks: list[dict[str, Any]] = Field(default_factory=list)
    identity: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)
    error_count: int = Field(default=0, ge=0)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    failure_class: str | None = None
    redaction: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentControlPlaneRunSummary(BaseModel):
    """Run summary composed from gateway records and trace summaries."""

    schema_version: str = "agent_control_plane_summary_v1"
    run_id: str
    trace_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    status: str
    source: str
    route_decision: dict[str, Any] = Field(default_factory=dict)
    delegated_tasks: list[dict[str, Any]] = Field(default_factory=list)
    identity: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)
    failure_class: str | None = None
    error_count: int = Field(default=0, ge=0)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)


class AgentControlPlaneRouteSummary(BaseModel):
    """Gateway route decision for one run."""

    schema_version: str = "agent_control_plane_route_v1"
    run_id: str
    trace_id: str | None = None
    route_decision: dict[str, Any] = Field(default_factory=dict)
    route_status: str | None = None
    failure_class: str | None = None
    redaction: dict[str, Any] = Field(default_factory=dict)


class AgentControlPlaneDelegationNode(BaseModel):
    """One node in a redacted delegation tree."""

    run_id: str | None = None
    trace_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    status: str | None = None
    artifact_count: int = Field(default=0, ge=0)
    error_codes: list[str] = Field(default_factory=list)


class AgentControlPlaneDelegationTree(BaseModel):
    """Parent/child delegation tree for a gateway run."""

    schema_version: str = "agent_control_plane_delegation_tree_v1"
    parent_run_id: str
    parent_trace_id: str | None = None
    root: AgentControlPlaneDelegationNode
    children: list[AgentControlPlaneDelegationNode] = Field(default_factory=list)
    redaction: dict[str, Any] = Field(default_factory=dict)


class AgentControlPlaneBudgetSummary(BaseModel):
    """Budget, cost, and latency summary for one run."""

    schema_version: str = "agent_control_plane_budget_v1"
    run_id: str
    trace_id: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)
    redaction: dict[str, Any] = Field(default_factory=dict)


class AgentControlPlaneReplayPreview(BaseModel):
    """Preview-only replay payload for a run."""

    schema_version: str = "agent_control_plane_replay_preview_v1"
    run_id: str
    trace_id: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    route_decision: dict[str, Any] = Field(default_factory=dict)
    delegated_tasks: list[dict[str, Any]] = Field(default_factory=list)
    failure_class: str | None = None
    replay_notes: list[str] = Field(default_factory=list)
    redaction: dict[str, Any] = Field(default_factory=dict)


class AgentAuditEvent(BaseModel):
    """Redacted audit event for control-plane decisions and user-scoped actions."""

    schema_version: str = "agent_audit_event_v1"
    event_id: str = Field(default_factory=lambda: f"audit_{uuid4().hex}")
    event_type: str = Field(min_length=1)
    component: str = Field(min_length=1)
    action: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    user_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    correlation_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentAuditEventList(BaseModel):
    """List response for redacted control-plane audit events."""

    schema_version: str = "agent_audit_event_list_v1"
    total: int = Field(default=0, ge=0)
    events: list[AgentAuditEvent] = Field(default_factory=list)
    retention: dict[str, Any] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)
