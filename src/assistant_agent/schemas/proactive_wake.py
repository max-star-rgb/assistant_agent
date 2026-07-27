from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.durable_tasks import TaskResumeRequest
from assistant_agent.schemas.notifications import (
    DeliveryResult as DeliveryResult,
    DeliveryStatus as DeliveryStatus,
    NotificationEnvelope,
    NotificationOwner,
)

WakeSignalKind = Literal["provider_event", "reconcile_tick", "manual"]
WakeConditionMode = Literal["changed", "semantic"]
WakeDecisionOutcome = Literal["silent", "notify", "resume"]
AttentionOutcome = Literal["allow", "defer", "suppress"]
WakeRunStatus = Literal[
    "received",
    "deduplicated",
    "config_error",
    "probing",
    "probe_failed",
    "baseline_established",
    "unchanged",
    "notify_candidate",
    "suppressed",
    "enqueued",
    "resume_requested",
    "delivered",
    "delivery_failed",
]
Severity = Literal["low", "normal", "high"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


WakeOwner = NotificationOwner


class WakeTriggerSpec(BaseModel):
    event_sources: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    reconcile_interval_s: int = Field(default=3600, ge=60)


class WakeProbeSpec(BaseModel):
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class WakeConditionSpec(BaseModel):
    mode: WakeConditionMode = "changed"
    notify_when: str = Field(min_length=1, max_length=500)
    notify_on_initial: bool = False


class QuietHours(BaseModel):
    start_local: time
    end_local: time
    timezone: str = Field(default="Asia/Shanghai", min_length=1)


class WakeAttentionSpec(BaseModel):
    channel: str = Field(default="mock_app", min_length=1)
    quiet_hours: QuietHours | None = None
    cooldown_s: int = Field(default=1800, ge=0)
    daily_notification_limit: int = Field(default=6, ge=1, le=100)
    minimum_severity: Severity = "normal"


class WakeResumeTarget(BaseModel):
    task_id: str = Field(min_length=1)
    expected_task_version: int = Field(ge=1)
    wait_id: str = Field(min_length=1)


class WakeRule(BaseModel):
    rule_id: str = Field(default_factory=lambda: _id("wake_rule"), min_length=1)
    owner: WakeOwner
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    version: int = Field(default=1, ge=1)
    trigger: WakeTriggerSpec
    probe: WakeProbeSpec
    condition: WakeConditionSpec
    attention: WakeAttentionSpec = Field(default_factory=WakeAttentionSpec)
    resume_target: WakeResumeTarget | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WakeSignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: _id("wake_signal"), min_length=1)
    kind: WakeSignalKind
    source: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    owner: WakeOwner
    event_key: str | None = None
    cursor: str | None = None
    prompt_safe_facts: dict[str, Any] = Field(default_factory=dict)


class WakeEvidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: _id("wake_evidence"), min_length=1)
    rule_id: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=utc_now)
    probe_tool_name: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "timed_out"]
    fingerprint: str = Field(min_length=1)
    previous_fingerprint: str | None = None
    is_initial: bool
    changed: bool
    summary: str = Field(min_length=1, max_length=500)
    prompt_safe_payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)


class WakeDecision(BaseModel):
    outcome: WakeDecisionOutcome
    severity: Severity
    reason_code: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
    user_message: str | None = Field(default=None, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "WakeDecision":
        if self.outcome in {"silent", "resume"} and self.user_message is not None:
            raise ValueError(f"{self.outcome} decision must not include user_message")
        if self.outcome == "notify" and (not self.user_message or not self.evidence_ids):
            raise ValueError("notify decision requires user_message and evidence_ids")
        if self.outcome == "resume" and not self.evidence_ids:
            raise ValueError("resume decision requires evidence_ids")
        return self


class AttentionDecision(BaseModel):
    outcome: AttentionOutcome
    reason_code: str = Field(min_length=1)
    deliver_after: datetime | None = None
    expires_at: datetime | None = None


class WakeRuleState(BaseModel):
    rule_id: str = Field(min_length=1)
    last_fingerprint: str | None = None
    last_checked_at: datetime | None = None
    last_notified_at: datetime | None = None
    last_notified_fingerprint: str | None = None
    next_reconcile_at: datetime | None = None
    notification_count_date: date | None = None
    notification_count: int = Field(default=0, ge=0)


class WakeRun(BaseModel):
    run_id: str = Field(default_factory=lambda: _id("wake_run"), min_length=1)
    rule_id: str = Field(min_length=1)
    owner: WakeOwner
    signal_id: str = Field(min_length=1)
    status: WakeRunStatus = "received"
    reason_code: str | None = None
    evidence: WakeEvidence | None = None
    decision: WakeDecision | None = None
    attention: AttentionDecision | None = None
    delivery_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProactiveWakeRunResult(BaseModel):
    run: WakeRun
    notification: NotificationEnvelope | None = None
    resume_request: TaskResumeRequest | None = None
