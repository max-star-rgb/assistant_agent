"""Deterministic proactive wake decisions and attention policy."""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant_agent.schemas.proactive_wake import (
    AttentionDecision,
    NotificationEnvelope,
    QuietHours,
    WakeDecision,
    WakeEvidence,
    WakeRule,
    WakeRuleState,
)
from assistant_agent.services.provider_errors import sanitize_error_message

_SEVERITY_RANK = {"low": 0, "normal": 1, "high": 2}


class DeterministicWakeEvaluator:
    """Turn fingerprint evidence into a deterministic wake decision."""

    def evaluate(self, *, rule: WakeRule, evidence: WakeEvidence, now: datetime) -> WakeDecision:
        if evidence.is_initial and not rule.condition.notify_on_initial:
            return WakeDecision(
                outcome="silent",
                severity="normal",
                reason_code="baseline_established",
                summary="Initial evidence baseline established.",
                evidence_ids=[evidence.evidence_id],
            )
        if not evidence.changed and not (evidence.is_initial and rule.condition.notify_on_initial):
            return WakeDecision(
                outcome="silent",
                severity="normal",
                reason_code="unchanged",
                summary="Evidence fingerprint is unchanged.",
                evidence_ids=[evidence.evidence_id],
            )
        message = sanitize_error_message(f"{rule.name}：{evidence.summary}")[:500]
        return WakeDecision(
            outcome="notify",
            severity="normal",
            reason_code=(
                "evidence_changed" if not evidence.is_initial else "initial_notification_enabled"
            ),
            summary=evidence.summary,
            user_message=message,
            evidence_ids=[evidence.evidence_id],
            expires_at=now + timedelta(hours=6),
        )


class AttentionPolicy:
    """Apply deterministic notification suppression and deferral gates."""

    def evaluate(
        self,
        *,
        rule: WakeRule,
        decision: WakeDecision,
        evidence: WakeEvidence,
        state: WakeRuleState,
        now: datetime,
        user_active: bool,
    ) -> AttentionDecision:
        if not rule.enabled:
            return _suppress("rule_disabled", decision)
        if decision.outcome != "notify":
            return _suppress("decision_not_notify", decision)
        if state.last_notified_fingerprint == evidence.fingerprint:
            return _suppress("duplicate_evidence", decision)
        if state.last_notified_at is not None:
            cooldown_ends_at = state.last_notified_at + timedelta(seconds=rule.attention.cooldown_s)
            if now < cooldown_ends_at:
                return _suppress("cooldown_active", decision)

        try:
            local_timezone = _local_timezone(rule.attention.quiet_hours)
        except (ValueError, ZoneInfoNotFoundError):
            return _suppress("policy_invalid_timezone", decision)
        local_now = now.astimezone(local_timezone)

        if (
            state.notification_count_date == local_now.date()
            and state.notification_count >= rule.attention.daily_notification_limit
        ):
            return _suppress("daily_limit_reached", decision)
        if decision.expires_at is not None and decision.expires_at <= now:
            return _suppress("decision_expired", decision)
        if _SEVERITY_RANK[decision.severity] < _SEVERITY_RANK[rule.attention.minimum_severity]:
            return _suppress("severity_below_minimum", decision)
        if user_active:
            return AttentionDecision(
                outcome="defer",
                reason_code="active_conversation",
                deliver_after=now + timedelta(seconds=60),
                expires_at=decision.expires_at,
            )

        quiet_end = _quiet_end(rule.attention.quiet_hours, local_now)
        if quiet_end is not None:
            return AttentionDecision(
                outcome="defer",
                reason_code="quiet_hours",
                deliver_after=quiet_end,
                expires_at=decision.expires_at,
            )
        return AttentionDecision(
            outcome="allow",
            reason_code="allowed",
            deliver_after=now,
            expires_at=decision.expires_at,
        )


def build_notification_envelope(
    *,
    rule: WakeRule,
    evidence: WakeEvidence,
    decision: WakeDecision,
    attention: AttentionDecision,
    now: datetime,
) -> NotificationEnvelope:
    """Build a transport-neutral notification with stable idempotency."""

    if not decision.user_message:
        raise ValueError("notification decision must include a user message")
    owner = rule.owner
    idempotency_key = hashlib.sha256(
        (
            f"{owner.user_id}|{owner.agent_id}|{rule.rule_id}|"
            f"{evidence.fingerprint}|{rule.attention.channel}"
        ).encode()
    ).hexdigest()
    return NotificationEnvelope(
        owner=owner,
        channel=rule.attention.channel,
        destination_ref=f"user:{owner.user_id}",
        message=decision.user_message,
        idempotency_key=idempotency_key,
        rule_id=rule.rule_id,
        evidence_ids=decision.evidence_ids,
        evidence_fingerprint=evidence.fingerprint,
        deliver_after=attention.deliver_after or now,
        expires_at=decision.expires_at or now + timedelta(hours=6),
    )


def _suppress(reason_code: str, decision: WakeDecision) -> AttentionDecision:
    return AttentionDecision(
        outcome="suppress",
        reason_code=reason_code,
        expires_at=decision.expires_at,
    )


def _local_timezone(quiet_hours: QuietHours | None) -> tzinfo:
    if quiet_hours is None:
        return timezone.utc
    return ZoneInfo(quiet_hours.timezone)


def _quiet_end(quiet_hours: QuietHours | None, local_now: datetime) -> datetime | None:
    if quiet_hours is None or quiet_hours.start_local == quiet_hours.end_local:
        return None
    current = _naive_time(local_now.timetz())
    start = _naive_time(quiet_hours.start_local)
    end = _naive_time(quiet_hours.end_local)
    if start < end:
        if not start <= current < end:
            return None
        end_date = local_now.date()
    elif current >= start:
        end_date = local_now.date() + timedelta(days=1)
    elif current < end:
        end_date = local_now.date()
    else:
        return None
    local_end = datetime.combine(end_date, end, tzinfo=local_now.tzinfo)
    return local_end.astimezone(timezone.utc)


def _naive_time(value: time) -> time:
    return value.replace(tzinfo=None)
