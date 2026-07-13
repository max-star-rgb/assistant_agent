"""Deterministic orchestration for one proactive wake rule run."""

from __future__ import annotations

import asyncio
import _thread
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from assistant_agent.schemas.proactive_wake import (
    ProactiveWakeRunResult,
    WakeOwner,
    WakeRule,
    WakeRuleState,
    WakeRun,
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


@dataclass
class _ProcessRuleLockEntry:
    lock: _thread.LockType = field(default_factory=threading.Lock)
    users: int = 0


_PROCESS_RULE_LOCKS: dict[_RuleLockKey, _ProcessRuleLockEntry] = {}
_PROCESS_RULE_LOCKS_GUARD = threading.Lock()
_PROCESS_RULE_LOCK_POLL_S = 0.001


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

        lock_key = _lock_key(owner, rule_id)
        async with _hold_process_rule_lock(lock_key):
            run, claimed = self.store.begin_run(rule, signal)
            if not claimed:
                return ProactiveWakeRunResult(run=run)

            try:
                state = self.store.get_rule_state(rule.rule_id)
            except BaseException:
                self._terminalize_failure(
                    run=run,
                    state=WakeRuleState(rule_id=rule.rule_id),
                    reason_code="proactive_state_read_failed",
                )
                raise
            active_run = run
            failure_stage = "validation"
            try:
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
                active_run = probing
                self.store.complete_run(probing, state)
                failure_stage = "probe"
                probe_task = asyncio.create_task(
                    asyncio.to_thread(self.probe_runner.run, rule, signal)
                )
                try:
                    observation = await asyncio.shield(probe_task)
                except asyncio.CancelledError:
                    await _await_background_task(probe_task, suppress_exceptions=True)
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
                failure_stage = "evidence"
                evidence = build_wake_evidence(
                    rule=rule,
                    observation=observation,
                    state=state,
                    observed_at=now,
                )
                failure_stage = "evaluator"
                decision = self.evaluator.evaluate(rule=rule, evidence=evidence, now=now)
                failure_stage = "activity"
                user_active = await self.activity_reader.is_active(owner)
                failure_stage = "attention"
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
                active_run = outcome_run

                if decision.outcome == "silent":
                    completed = outcome_run.model_copy(
                        update={
                            "status": decision.reason_code,
                            "reason_code": decision.reason_code,
                        }
                    )
                    failure_stage = "persistence"
                    persisted, _ = self.store.complete_outcome(
                        run=completed,
                        state=successful_state,
                        notification=None,
                    )
                    return ProactiveWakeRunResult(run=persisted)

                if attention.outcome == "suppress":
                    completed = outcome_run.model_copy(
                        update={
                            "status": "suppressed",
                            "reason_code": attention.reason_code,
                        }
                    )
                    failure_stage = "persistence"
                    persisted, _ = self.store.complete_outcome(
                        run=completed,
                        state=successful_state,
                        notification=None,
                    )
                    return ProactiveWakeRunResult(run=persisted)

                failure_stage = "envelope"
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
                failure_stage = "persistence"
                persisted, actual_notification = self.store.complete_outcome(
                    run=completed,
                    state=notified_state,
                    notification=notification,
                )
                return ProactiveWakeRunResult(
                    run=persisted,
                    notification=actual_notification,
                )
            except asyncio.CancelledError:
                self._terminalize_failure(
                    run=active_run,
                    state=state,
                    reason_code="proactive_run_cancelled",
                )
                raise
            except Exception:
                self._terminalize_failure(
                    run=active_run,
                    state=state,
                    reason_code=f"proactive_{failure_stage}_failed",
                )
                raise

    def _terminalize_failure(
        self,
        *,
        run: WakeRun,
        state: WakeRuleState,
        reason_code: str,
    ) -> None:
        try:
            failed = run.model_copy(
                update={
                    "status": "probe_failed",
                    "reason_code": reason_code,
                    "updated_at": self.now_fn(),
                }
            )
            self.store.complete_outcome(run=failed, state=state, notification=None)
        except BaseException:
            return


def _lock_key(owner: WakeOwner, rule_id: str) -> _RuleLockKey:
    return owner.tenant_id, owner.user_id, owner.project_id, rule_id


def _retain_process_rule_lock(key: _RuleLockKey) -> _ProcessRuleLockEntry:
    with _PROCESS_RULE_LOCKS_GUARD:
        entry = _PROCESS_RULE_LOCKS.setdefault(key, _ProcessRuleLockEntry())
        entry.users += 1
        return entry


def _release_process_rule_lock(key: _RuleLockKey, entry: _ProcessRuleLockEntry) -> None:
    with _PROCESS_RULE_LOCKS_GUARD:
        entry.users -= 1
        if entry.users == 0 and _PROCESS_RULE_LOCKS.get(key) is entry:
            del _PROCESS_RULE_LOCKS[key]


@asynccontextmanager
async def _hold_process_rule_lock(key: _RuleLockKey) -> AsyncIterator[None]:
    entry = _retain_process_rule_lock(key)
    acquired = False
    try:
        while not entry.lock.acquire(blocking=False):
            await asyncio.sleep(_PROCESS_RULE_LOCK_POLL_S)
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        _release_process_rule_lock(key, entry)


async def _await_background_task(task, *, suppress_exceptions: bool = False):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            if suppress_exceptions:
                return None
            raise
    try:
        return task.result()
    except BaseException:
        if suppress_exceptions:
            return None
        raise


def _notification_local_date(rule: WakeRule, now: datetime) -> date:
    quiet_hours = rule.attention.quiet_hours
    local_timezone = ZoneInfo(quiet_hours.timezone) if quiet_hours is not None else timezone.utc
    return now.astimezone(local_timezone).date()
