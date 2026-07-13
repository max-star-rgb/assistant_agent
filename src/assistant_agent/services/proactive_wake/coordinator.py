"""Deterministic orchestration for one proactive wake rule run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from assistant_agent.schemas.proactive_wake import (
    ProactiveWakeRunResult,
    WakeOwner,
    WakeRule,
    WakeSignal,
    utc_now,
)
from assistant_agent.services.proactive_wake.activity import (
    NullUserActivityReader,
    UserActivityReader,
)
from assistant_agent.services.proactive_wake.change_detector import build_wake_evidence
from assistant_agent.services.proactive_wake.policy import (
    AttentionPolicy,
    DeterministicWakeEvaluator,
    build_notification_envelope,
)
from assistant_agent.services.proactive_wake.probe import (
    GovernedProbeRunner,
    ProactiveRuleValidator,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore

_RuleLockKey = tuple[str | None, str, str | None, str]
_PROCESS_RULE_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[_RuleLockKey, asyncio.Lock]
] = WeakKeyDictionary()


class ProactiveWakeError(RuntimeError):
    """Structured coordinator rejection raised before or during a wake run."""

    def __init__(self, *, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class ProactiveWakeCoordinator:
    def __init__(
        self,
        *,
        store: SQLiteProactiveWakeStore,
        rule_validator: ProactiveRuleValidator,
        probe_runner: GovernedProbeRunner,
        evaluator: DeterministicWakeEvaluator | None = None,
        attention_policy: AttentionPolicy | None = None,
        activity_reader: UserActivityReader | None = None,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.rule_validator = rule_validator
        self.probe_runner = probe_runner
        self.evaluator = evaluator or DeterministicWakeEvaluator()
        self.attention_policy = attention_policy or AttentionPolicy()
        self.activity_reader = activity_reader or NullUserActivityReader()
        self.now_fn = now_fn

    def save_rule(self, rule: WakeRule) -> WakeRule:
        validation = self.rule_validator.validate(rule)
        if not validation.accepted:
            raise ProactiveWakeError(code=validation.code, message=validation.message)
        return self.store.save_rule(rule)

    async def run_rule(
        self,
        *,
        rule_id: str,
        owner: WakeOwner,
        signal: WakeSignal,
    ) -> ProactiveWakeRunResult:
        rule = self.store.get_rule(owner, rule_id)
        if rule is None:
            raise ProactiveWakeError(code="rule_not_found")
        if signal.owner != owner or owner != rule.owner:
            raise ProactiveWakeError(code="signal_owner_mismatch")
        if signal.kind == "provider_event" and (
            signal.source not in rule.trigger.event_sources
            or signal.event_type not in rule.trigger.event_types
        ):
            raise ProactiveWakeError(code="signal_not_matched")

        loop_locks = _PROCESS_RULE_LOCKS.setdefault(asyncio.get_running_loop(), {})
        lock = loop_locks.setdefault(_lock_key(owner, rule_id), asyncio.Lock())
        async with lock:
            run, claimed = self.store.begin_run(rule, signal)
            if not claimed:
                return ProactiveWakeRunResult(run=run)

            state = self.store.get_rule_state(rule.rule_id)
            validation = self.rule_validator.validate(rule)
            if not validation.accepted:
                completed = run.model_copy(
                    update={
                        "status": "config_error",
                        "reason_code": validation.code,
                        "updated_at": self.now_fn(),
                    }
                )
                persisted, _ = self.store.complete_outcome(
                    run=completed,
                    state=state,
                    notification=None,
                )
                return ProactiveWakeRunResult(run=persisted)

            probing = run.model_copy(
                update={"status": "probing", "updated_at": self.now_fn()}
            )
            self.store.complete_run(probing, state)
            probe_task = asyncio.create_task(
                asyncio.to_thread(self.probe_runner.run, rule, signal)
            )
            try:
                observation = await asyncio.shield(probe_task)
            except asyncio.CancelledError:
                await probe_task
                raise
            if not observation.success:
                failed = probing.model_copy(
                    update={
                        "status": "probe_failed",
                        "reason_code": observation.code,
                        "updated_at": self.now_fn(),
                    }
                )
                persisted, _ = self.store.complete_outcome(
                    run=failed,
                    state=state,
                    notification=None,
                )
                return ProactiveWakeRunResult(run=persisted)

            now = self.now_fn()
            evidence = build_wake_evidence(
                rule=rule,
                observation=observation,
                state=state,
                observed_at=now,
            )
            decision = self.evaluator.evaluate(rule=rule, evidence=evidence, now=now)
            user_active = await self.activity_reader.is_active(owner)
            attention = self.attention_policy.evaluate(
                rule=rule,
                decision=decision,
                evidence=evidence,
                state=state,
                now=now,
                user_active=user_active,
            )
            successful_state = state.model_copy(
                update={
                    "last_fingerprint": evidence.fingerprint,
                    "last_checked_at": now,
                    "next_reconcile_at": now
                    + timedelta(seconds=rule.trigger.reconcile_interval_s),
                }
            )
            outcome_run = probing.model_copy(
                update={
                    "evidence": evidence,
                    "decision": decision,
                    "attention": attention,
                    "updated_at": now,
                }
            )

            if decision.outcome == "silent":
                completed = outcome_run.model_copy(
                    update={"status": decision.reason_code, "reason_code": decision.reason_code}
                )
                persisted, _ = self.store.complete_outcome(
                    run=completed,
                    state=successful_state,
                    notification=None,
                )
                return ProactiveWakeRunResult(run=persisted)

            if attention.outcome == "suppress":
                completed = outcome_run.model_copy(
                    update={"status": "suppressed", "reason_code": attention.reason_code}
                )
                persisted, _ = self.store.complete_outcome(
                    run=completed,
                    state=successful_state,
                    notification=None,
                )
                return ProactiveWakeRunResult(run=persisted)

            notification = build_notification_envelope(
                rule=rule,
                evidence=evidence,
                decision=decision,
                attention=attention,
                now=now,
            )
            local_date = _notification_local_date(rule, now)
            count = (
                successful_state.notification_count + 1
                if successful_state.notification_count_date == local_date
                else 1
            )
            notified_state = successful_state.model_copy(
                update={
                    "last_notified_at": now,
                    "last_notified_fingerprint": evidence.fingerprint,
                    "notification_count_date": local_date,
                    "notification_count": count,
                }
            )
            completed = outcome_run.model_copy(
                update={
                    "status": "enqueued",
                    "reason_code": attention.reason_code,
                    "delivery_id": notification.delivery_id,
                }
            )
            persisted, actual_notification = self.store.complete_outcome(
                run=completed,
                state=notified_state,
                notification=notification,
            )
            return ProactiveWakeRunResult(
                run=persisted,
                notification=actual_notification,
            )


def _lock_key(owner: WakeOwner, rule_id: str) -> _RuleLockKey:
    return owner.tenant_id, owner.user_id, owner.project_id, rule_id


def _notification_local_date(rule: WakeRule, now: datetime) -> date:
    quiet_hours = rule.attention.quiet_hours
    local_timezone = ZoneInfo(quiet_hours.timezone) if quiet_hours is not None else timezone.utc
    return now.astimezone(local_timezone).date()
