import hashlib
from datetime import date, datetime, time, timedelta, timezone

import pytest

from assistant_agent.schemas.proactive_wake import (
    AttentionDecision,
    QuietHours,
    WakeAttentionSpec,
    WakeConditionSpec,
    WakeDecision,
    WakeEvidence,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeRuleState,
    WakeTriggerSpec,
)
from assistant_agent.services.proactive_wake.policy import (
    AttentionPolicy,
    DeterministicWakeEvaluator,
    build_notification_envelope,
)
from assistant_agent.services.provider_errors import sanitize_error_message


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def make_rule(
    *,
    enabled: bool = True,
    notify_on_initial: bool = False,
    attention: WakeAttentionSpec | None = None,
) -> WakeRule:
    return WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(tenant_id="tenant-1", user_id="user-1", project_id="project-1"),
        name="Calendar changes",
        enabled=enabled,
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(tool_name="calendar.search_events"),
        condition=WakeConditionSpec(
            mode="changed",
            notify_when="Calendar evidence changes",
            notify_on_initial=notify_on_initial,
        ),
        attention=attention or WakeAttentionSpec(),
    )


def make_evidence(
    rule: WakeRule,
    *,
    fingerprint: str = "fingerprint-new",
    previous_fingerprint: str | None = "fingerprint-old",
    is_initial: bool = False,
    changed: bool = True,
    summary: str = "Calendar event changed.",
) -> WakeEvidence:
    return WakeEvidence(
        evidence_id="evidence-1",
        rule_id=rule.rule_id,
        observed_at=NOW,
        probe_tool_name=rule.probe.tool_name,
        status="succeeded",
        fingerprint=fingerprint,
        previous_fingerprint=previous_fingerprint,
        is_initial=is_initial,
        changed=changed,
        summary=summary,
    )


def make_notify_decision(
    *,
    severity: str = "normal",
    expires_at: datetime | None = NOW + timedelta(hours=6),
) -> WakeDecision:
    return WakeDecision(
        outcome="notify",
        severity=severity,
        reason_code="evidence_changed",
        summary="Calendar event changed.",
        user_message="Calendar changes：Calendar event changed.",
        evidence_ids=["evidence-1"],
        expires_at=expires_at,
    )


def make_quiet_attention(*, timezone_name: str = "Asia/Shanghai") -> WakeAttentionSpec:
    return WakeAttentionSpec(
        cooldown_s=0,
        quiet_hours=QuietHours(
            start_local=time(23, 0),
            end_local=time(8, 0),
            timezone=timezone_name,
        ),
    )


def evaluate_attention(
    *,
    rule: WakeRule,
    evidence: WakeEvidence | None = None,
    decision: WakeDecision | None = None,
    state: WakeRuleState | None = None,
    now: datetime = NOW,
    user_active: bool = False,
) -> AttentionDecision:
    return AttentionPolicy().evaluate(
        rule=rule,
        evidence=evidence or make_evidence(rule),
        decision=decision or make_notify_decision(),
        state=state or WakeRuleState(rule_id=rule.rule_id),
        now=now,
        user_active=user_active,
    )


def test_initial_evidence_establishes_silent_baseline_by_default() -> None:
    rule = make_rule()
    evidence = make_evidence(
        rule,
        previous_fingerprint=None,
        is_initial=True,
        changed=False,
    )

    decision = DeterministicWakeEvaluator().evaluate(rule=rule, evidence=evidence, now=NOW)

    assert decision.outcome == "silent"
    assert decision.reason_code == "baseline_established"
    assert decision.user_message is None


def test_changed_evidence_creates_one_notify_candidate() -> None:
    rule = make_rule()
    evidence = make_evidence(rule)

    decision = DeterministicWakeEvaluator().evaluate(rule=rule, evidence=evidence, now=NOW)

    assert decision.outcome == "notify"
    assert decision.reason_code == "evidence_changed"
    assert decision.evidence_ids == [evidence.evidence_id]
    assert decision.expires_at == NOW + timedelta(hours=6)
    assert decision.user_message is not None
    assert len(decision.user_message) <= 500


def test_unchanged_evidence_is_silent() -> None:
    rule = make_rule()
    evidence = make_evidence(
        rule,
        fingerprint="same-fingerprint",
        previous_fingerprint="same-fingerprint",
        changed=False,
    )

    decision = DeterministicWakeEvaluator().evaluate(rule=rule, evidence=evidence, now=NOW)

    assert decision.outcome == "silent"
    assert decision.reason_code == "unchanged"
    assert decision.user_message is None


def test_initial_evidence_can_opt_in_to_notification() -> None:
    rule = make_rule(notify_on_initial=True)
    evidence = make_evidence(
        rule,
        previous_fingerprint=None,
        is_initial=True,
        changed=False,
    )

    decision = DeterministicWakeEvaluator().evaluate(rule=rule, evidence=evidence, now=NOW)

    assert decision.outcome == "notify"
    assert decision.reason_code == "initial_notification_enabled"
    assert decision.evidence_ids == [evidence.evidence_id]


def test_duplicate_notified_fingerprint_is_suppressed() -> None:
    rule = make_rule()
    evidence = make_evidence(rule)
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_fingerprint=evidence.fingerprint,
    )

    attention = evaluate_attention(rule=rule, evidence=evidence, state=state)

    assert attention.outcome == "suppress"
    assert attention.reason_code == "duplicate_evidence"


@pytest.mark.parametrize(
    ("state", "reason_code"),
    [
        (
            WakeRuleState(
                rule_id="rule-1",
                last_notified_at=NOW - timedelta(seconds=30),
            ),
            "cooldown_active",
        ),
        (
            WakeRuleState(
                rule_id="rule-1",
                notification_count_date=date(2026, 7, 13),
                notification_count=6,
            ),
            "daily_limit_reached",
        ),
    ],
    ids=["cooldown", "daily-limit"],
)
def test_cooldown_and_daily_limit_are_suppressed(state, reason_code) -> None:
    attention = evaluate_attention(rule=make_rule(), state=state)

    assert attention.outcome == "suppress"
    assert attention.reason_code == reason_code


def test_quiet_hours_defer_until_local_end() -> None:
    now = datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc)
    rule = make_rule(attention=make_quiet_attention())

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(expires_at=now + timedelta(hours=12)),
        now=now,
    )

    assert attention.outcome == "defer"
    assert attention.reason_code == "quiet_hours"
    assert attention.deliver_after == datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)


def test_overnight_quiet_hours_are_supported() -> None:
    rule = make_rule(attention=make_quiet_attention())
    cases = [
        (datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc), "defer"),
        (datetime(2026, 7, 13, 23, 30, tzinfo=timezone.utc), "defer"),
        (datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc), "allow"),
    ]

    outcomes = [
        evaluate_attention(
            rule=rule,
            decision=make_notify_decision(expires_at=now + timedelta(hours=12)),
            now=now,
        ).outcome
        for now, _ in cases
    ]

    assert outcomes == [expected for _, expected in cases]


@pytest.mark.parametrize(
    ("now", "expected_outcome"),
    [
        (datetime(2026, 7, 13, 14, 59, 59, tzinfo=timezone.utc), "allow"),
        (datetime(2026, 7, 13, 15, 0, 0, tzinfo=timezone.utc), "defer"),
        (datetime(2026, 7, 13, 23, 59, 59, tzinfo=timezone.utc), "defer"),
        (datetime(2026, 7, 14, 0, 0, 0, tzinfo=timezone.utc), "allow"),
    ],
    ids=["before-start", "at-start", "before-end", "at-end"],
)
def test_quiet_hours_are_start_inclusive_and_end_exclusive(now, expected_outcome) -> None:
    rule = make_rule(attention=make_quiet_attention())

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(expires_at=now + timedelta(hours=12)),
        now=now,
    )

    assert attention.outcome == expected_outcome


def test_active_realtime_run_defers_for_sixty_seconds() -> None:
    attention = evaluate_attention(rule=make_rule(), user_active=True)

    assert attention.outcome == "defer"
    assert attention.reason_code == "active_conversation"
    assert attention.deliver_after == NOW + timedelta(seconds=60)


def test_notification_message_uses_rule_name_and_sanitized_summary() -> None:
    rule = make_rule()
    unsafe_summary = "Calendar changed api_key=sk-private-secret before next meeting."
    evidence = make_evidence(rule, summary=unsafe_summary)

    decision = DeterministicWakeEvaluator().evaluate(rule=rule, evidence=evidence, now=NOW)

    assert sanitize_error_message(f"{rule.name}：{unsafe_summary}") == decision.user_message
    assert decision.user_message is not None
    assert decision.user_message.startswith(rule.name)
    assert "sk-private-secret" not in decision.user_message
    assert "[redacted]" in decision.user_message


def test_disabled_rule_precedes_all_later_attention_gates() -> None:
    rule = make_rule(enabled=False, attention=make_quiet_attention(timezone_name="Invalid/Zone"))
    evidence = make_evidence(rule)
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_fingerprint=evidence.fingerprint,
        last_notified_at=NOW,
        notification_count_date=NOW.date(),
        notification_count=6,
    )

    attention = evaluate_attention(
        rule=rule,
        evidence=evidence,
        decision=make_notify_decision(expires_at=NOW),
        state=state,
        user_active=True,
    )

    assert (attention.outcome, attention.reason_code) == ("suppress", "rule_disabled")


def test_silent_decision_precedes_duplicate_and_timezone_gates() -> None:
    rule = make_rule(attention=make_quiet_attention(timezone_name="Invalid/Zone"))
    evidence = make_evidence(rule)
    decision = DeterministicWakeEvaluator().evaluate(
        rule=rule,
        evidence=evidence.model_copy(update={"changed": False}),
        now=NOW,
    )
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_fingerprint=evidence.fingerprint,
    )

    attention = evaluate_attention(
        rule=rule,
        evidence=evidence,
        decision=decision,
        state=state,
    )

    assert (attention.outcome, attention.reason_code) == ("suppress", "decision_not_notify")


def test_duplicate_precedes_cooldown_and_timezone_gates() -> None:
    rule = make_rule(attention=make_quiet_attention(timezone_name="Invalid/Zone"))
    evidence = make_evidence(rule)
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_fingerprint=evidence.fingerprint,
        last_notified_at=NOW,
    )

    attention = evaluate_attention(rule=rule, evidence=evidence, state=state)

    assert (attention.outcome, attention.reason_code) == ("suppress", "duplicate_evidence")


def test_listed_suppression_gates_precede_minimum_severity() -> None:
    rule = make_rule(attention=WakeAttentionSpec(minimum_severity="high"))
    evidence = make_evidence(rule)
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_fingerprint=evidence.fingerprint,
    )

    attention = evaluate_attention(
        rule=rule,
        evidence=evidence,
        decision=make_notify_decision(severity="low"),
        state=state,
    )

    assert (attention.outcome, attention.reason_code) == ("suppress", "duplicate_evidence")


def test_cooldown_precedes_daily_limit_and_timezone_gates() -> None:
    attention_spec = make_quiet_attention(timezone_name="Invalid/Zone").model_copy(
        update={"cooldown_s": 1800}
    )
    rule = make_rule(attention=attention_spec)
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_at=NOW - timedelta(seconds=1),
        notification_count_date=NOW.date(),
        notification_count=6,
    )

    attention = evaluate_attention(rule=rule, state=state)

    assert (attention.outcome, attention.reason_code) == ("suppress", "cooldown_active")


def test_cooldown_allows_at_exact_elapsed_boundary() -> None:
    rule = make_rule()
    state = WakeRuleState(
        rule_id=rule.rule_id,
        last_notified_at=NOW - timedelta(seconds=rule.attention.cooldown_s),
    )

    attention = evaluate_attention(rule=rule, state=state)

    assert (attention.outcome, attention.reason_code) == ("allow", "allowed")


def test_daily_limit_precedes_expiry_active_and_quiet_gates() -> None:
    rule = make_rule(attention=make_quiet_attention())
    now = datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc)
    state = WakeRuleState(
        rule_id=rule.rule_id,
        notification_count_date=date(2026, 7, 13),
        notification_count=6,
    )

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(expires_at=now),
        state=state,
        now=now,
        user_active=True,
    )

    assert (attention.outcome, attention.reason_code) == ("suppress", "daily_limit_reached")


def test_expired_decision_precedes_active_and_quiet_gates() -> None:
    now = datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc)
    rule = make_rule(attention=make_quiet_attention())

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(expires_at=now),
        now=now,
        user_active=True,
    )

    assert (attention.outcome, attention.reason_code) == ("suppress", "decision_expired")


def test_active_conversation_precedes_quiet_hours() -> None:
    now = datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc)
    rule = make_rule(attention=make_quiet_attention())

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(expires_at=now + timedelta(hours=12)),
        now=now,
        user_active=True,
    )

    assert attention.reason_code == "active_conversation"
    assert attention.deliver_after == now + timedelta(seconds=60)


@pytest.mark.parametrize("timezone_name", ["Invalid/Zone", "/absolute/timezone"])
def test_invalid_timezone_fails_closed(timezone_name) -> None:
    rule = make_rule(attention=make_quiet_attention(timezone_name=timezone_name))

    attention = evaluate_attention(rule=rule)

    assert (attention.outcome, attention.reason_code) == (
        "suppress",
        "policy_invalid_timezone",
    )


def test_daily_limit_resets_on_the_next_local_day() -> None:
    now = datetime(2026, 7, 13, 16, 30, tzinfo=timezone.utc)
    rule = make_rule(attention=make_quiet_attention())
    state = WakeRuleState(
        rule_id=rule.rule_id,
        notification_count_date=date(2026, 7, 13),
        notification_count=6,
    )

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(expires_at=now + timedelta(hours=12)),
        state=state,
        now=now,
    )

    assert (attention.outcome, attention.reason_code) == ("defer", "quiet_hours")


@pytest.mark.parametrize(
    ("severity", "minimum", "expected_outcome"),
    [
        ("low", "normal", "suppress"),
        ("normal", "normal", "allow"),
        ("high", "normal", "allow"),
        ("normal", "high", "suppress"),
    ],
)
def test_minimum_severity_uses_low_normal_high_order(severity, minimum, expected_outcome) -> None:
    rule = make_rule(attention=WakeAttentionSpec(minimum_severity=minimum, cooldown_s=0))

    attention = evaluate_attention(
        rule=rule,
        decision=make_notify_decision(severity=severity),
    )

    assert attention.outcome == expected_outcome
    if expected_outcome == "suppress":
        assert attention.reason_code == "severity_below_minimum"


def test_notification_envelope_uses_exact_stable_idempotency_fields() -> None:
    rule = make_rule()
    evidence = make_evidence(rule)
    decision = make_notify_decision()
    deliver_after = NOW + timedelta(minutes=5)
    attention = AttentionDecision(
        outcome="defer",
        reason_code="quiet_hours",
        deliver_after=deliver_after,
    )
    expected_key = hashlib.sha256(
        (
            f"{rule.owner.tenant_id}|{rule.owner.user_id}|{rule.owner.project_id}|"
            f"{rule.rule_id}|{evidence.fingerprint}|{rule.attention.channel}"
        ).encode()
    ).hexdigest()

    first = build_notification_envelope(
        rule=rule,
        evidence=evidence,
        decision=decision,
        attention=attention,
        now=NOW,
    )
    second = build_notification_envelope(
        rule=rule,
        evidence=evidence,
        decision=decision,
        attention=attention,
        now=NOW,
    )

    assert first.idempotency_key == second.idempotency_key == expected_key
    assert first.owner == rule.owner
    assert first.channel == rule.attention.channel
    assert first.destination_ref == "user:user-1"
    assert first.message == decision.user_message
    assert first.rule_id == rule.rule_id
    assert first.evidence_ids == decision.evidence_ids
    assert first.evidence_fingerprint == evidence.fingerprint
    assert first.deliver_after == deliver_after
    assert first.expires_at == decision.expires_at


def test_notification_envelope_defaults_delivery_and_expiry_from_now() -> None:
    rule = make_rule()
    evidence = make_evidence(rule)
    decision = make_notify_decision(expires_at=None)

    envelope = build_notification_envelope(
        rule=rule,
        evidence=evidence,
        decision=decision,
        attention=AttentionDecision(outcome="allow", reason_code="allowed"),
        now=NOW,
    )

    assert envelope.deliver_after == NOW
    assert envelope.expires_at == NOW + timedelta(hours=6)
