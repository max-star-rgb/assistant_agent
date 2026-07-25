"""Transport-neutral notification delivery contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID
from assistant_agent.schemas.identity import RequestIdentity

DeliveryStatus = Literal[
    "queued",
    "leased",
    "sent",
    "acknowledged",
    "retry_wait",
    "expired",
    "dead_letter",
]
NotificationOriginKind = Literal["proactive_wake", "durable_task"]


class NotificationOwner(BaseModel):
    user_id: str = Field(min_length=1)
    agent_id: str = Field(default=DEFAULT_AGENT_ID, min_length=1)

    @classmethod
    def from_identity(cls, identity: RequestIdentity) -> "NotificationOwner":
        return cls(
            user_id=identity.user_id,
            agent_id=identity.agent_id,
        )


class NotificationEnvelope(BaseModel):
    delivery_id: str = Field(
        default_factory=lambda: f"notification_{uuid4().hex}",
        min_length=1,
    )
    owner: NotificationOwner
    channel: str = Field(min_length=1)
    destination_ref: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    origin_kind: NotificationOriginKind = "proactive_wake"
    origin_ref: str | None = Field(default=None, min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    evidence_fingerprint: str = Field(min_length=1)
    deliver_after: datetime
    expires_at: datetime
    status: DeliveryStatus = "queued"
    attempt_count: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    provider_message_id: str | None = None
    last_reason_code: str | None = None

    @model_validator(mode="after")
    def validate_delivery_window(self) -> "NotificationEnvelope":
        if self.deliver_after.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("notification timestamps must be timezone-aware")
        if self.expires_at <= self.deliver_after:
            raise ValueError("expires_at must be later than deliver_after")
        if self.origin_ref is None:
            self.origin_ref = self.rule_id
        return self


class DeliveryResult(BaseModel):
    accepted: bool
    provider_message_id: str | None = None
    error_code: str | None = None
