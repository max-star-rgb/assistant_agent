import asyncio
import multiprocessing
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel, Field

import assistant_agent.services.proactive_wake.coordinator as coordinator_module
from assistant_agent.schemas.proactive_wake import (
    WakeAttentionSpec,
    WakeConditionSpec,
    WakeDecision,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeRuleState,
    WakeRun,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
)
from assistant_agent.services.proactive_wake.coordinator import (
    ProactiveWakeCoordinator,
    ProactiveWakeError,
)
from assistant_agent.services.proactive_wake.probe import (
    GovernedProbeRunner,
    ProactiveRuleValidator,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore
from assistant_agent.tools.decorators import tool
from assistant_agent.tools.registry import ToolRegistry


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
DEFAULT_EXECUTOR_WORKERS = min(32, (os.cpu_count() or 1) + 4)


class QueryInput(BaseModel):
    query: str = Field(min_length=1)


class StrictQueryInput(BaseModel):
    query: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)


class SequenceTool:
    def __init__(self, responses: list[dict[str, Any] | ToolResult], *, delay_s: float = 0) -> None:
        self.responses = responses
        self.delay_s = delay_s
        self.call_count = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

        @tool(
            name="calendar.search_events",
            description="Search calendar events.",
            input_schema=QueryInput,
            execution=ToolExecutionPolicy(
                dependency_mode="independent",
                resource_reads=["calendar.events"],
                realtime_safety="safe",
            ),
            policy=ToolPolicyMetadata(
                risk="external_read",
                approval=ApprovalPolicy(mode="never"),
            ),
        )
        def calendar_search_events(input, context):
            with self._lock:
                index = self.call_count
                self.call_count += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                if self.delay_s:
                    time.sleep(self.delay_s)
                response = self.responses[min(index, len(self.responses) - 1)]
                if isinstance(response, ToolResult):
                    return response
                return ToolResult(
                    tool_name="calendar.search_events",
                    success=True,
                    data={"provider_raw_response": "provider-private"},
                    model_observation=response,
                    raw_data_ref="calendar-raw://private",
                )
            finally:
                with self._lock:
                    self.active -= 1

        self.tool = calendar_search_events


class ActivityReader:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.calls: list[WakeOwner] = []

    async def is_active(self, owner: WakeOwner) -> bool:
        self.calls.append(owner)
        return self.active


class LockStressStore:
    """Thread-safe in-memory store isolating coordinator lock/executor behavior."""

    def __init__(self) -> None:
        self.rule = None
        self.state = None
        self.dedup_keys: set[str] = set()
        self.runs: dict[str, WakeRun] = {}
        self._lock = threading.Lock()

    def save_rule(self, rule):
        self.rule = rule
        self.state = WakeRuleState(rule_id=rule.rule_id)
        return rule

    def get_rule(self, owner, rule_id):
        if self.rule is not None and self.rule.owner == owner and self.rule.rule_id == rule_id:
            return self.rule
        return None

    def begin_run(self, rule, signal):
        dedup_key = signal.event_key or signal.signal_id
        with self._lock:
            claimed = dedup_key not in self.dedup_keys
            self.dedup_keys.add(dedup_key)
            run = WakeRun(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal_id=signal.signal_id,
                status="received" if claimed else "deduplicated",
            )
            self.runs[run.run_id] = run
            return run, claimed

    def get_rule_state(self, rule_id):
        return self.state

    def complete_run(self, run, state):
        completed, _ = self.complete_outcome(run=run, state=state, notification=None)
        return completed

    def complete_outcome(self, *, run, state, notification):
        safe_run = run.model_copy(update={"decision": None})
        with self._lock:
            self.state = state
            self.runs[run.run_id] = safe_run
        return safe_run, notification


def make_rule(
    *,
    rule_id: str = "rule-1",
    owner: WakeOwner | None = None,
    notify_on_initial: bool = False,
) -> WakeRule:
    return WakeRule(
        rule_id=rule_id,
        owner=owner
        or WakeOwner(tenant_id="tenant-1", user_id="user-1", project_id="project-1"),
        name="Calendar changes",
        trigger=WakeTriggerSpec(
            event_sources=["calendar"],
            event_types=["calendar.changed"],
            reconcile_interval_s=300,
        ),
        probe=WakeProbeSpec(
            tool_name="calendar.search_events",
            arguments={"query": "next two hours"},
        ),
        condition=WakeConditionSpec(
            mode="changed",
            notify_when="Calendar evidence changes",
            notify_on_initial=notify_on_initial,
        ),
        attention=WakeAttentionSpec(cooldown_s=0),
    )


def make_signal(
    rule: WakeRule,
    *,
    signal_id: str,
    event_key: str | None = None,
    owner: WakeOwner | None = None,
    source: str = "calendar",
    event_type: str = "calendar.changed",
) -> WakeSignal:
    return WakeSignal(
        signal_id=signal_id,
        kind="provider_event",
        source=source,
        event_type=event_type,
        event_key=event_key,
        owner=owner or rule.owner,
    )


def make_harness(tmp_path, responses, *, active: bool = False, delay_s: float = 0):
    sequence = SequenceTool(responses, delay_s=delay_s)
    registry = ToolRegistry()
    registry.register(sequence.tool)
    validator = ProactiveRuleValidator(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )
    runner = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )
    activity = ActivityReader(active)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    coordinator = ProactiveWakeCoordinator(
        store=store,
        rule_validator=validator,
        probe_runner=runner,
        activity_reader=activity,
        now_fn=lambda: NOW,
    )
    return coordinator, store, sequence, activity


def run_rule(coordinator, rule, signal):
    return asyncio.run(
        coordinator.run_rule(rule_id=rule.rule_id, owner=rule.owner, signal=signal)
    )


def run_executor_saturation_scenario(task_count: int, result_queue) -> None:
    try:
        sequence = SequenceTool([{"event": "baseline"}])
        probe_finished = threading.Event()
        original_handler = sequence.tool._handler

        def signal_probe_finished(input, context):
            try:
                return original_handler(input, context)
            finally:
                probe_finished.set()

        sequence.tool._handler = signal_probe_finished
        registry = ToolRegistry()
        registry.register(sequence.tool)
        validator = ProactiveRuleValidator(
            registry=registry,
            allowed_tool_names={"calendar.search_events"},
        )
        runner = GovernedProbeRunner(
            registry=registry,
            allowed_tool_names={"calendar.search_events"},
        )
        store = LockStressStore()
        coordinator = ProactiveWakeCoordinator(
            store=store,
            rule_validator=validator,
            probe_runner=runner,
            now_fn=lambda: NOW,
        )
        rule = coordinator.save_rule(make_rule())

        async def scenario():
            tasks = [
                asyncio.create_task(
                    coordinator.run_rule(
                        rule_id=rule.rule_id,
                        owner=rule.owner,
                        signal=make_signal(
                            rule,
                            signal_id=f"stress-signal-{index}",
                            event_key="shared-stress-event",
                        ),
                    )
                )
                for index in range(task_count)
            ]
            while not probe_finished.is_set():
                await asyncio.sleep(0.005)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(scenario())
        result_queue.put(
            {
                "call_count": sequence.call_count,
                "max_active": sequence.max_active,
                "registry_clean": not coordinator_module._PROCESS_RULE_LOCKS,
            }
        )
    except BaseException as exc:
        result_queue.put({"error": repr(exc)})


def test_first_probe_establishes_baseline_without_notification(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "baseline"}])
    rule = coordinator.save_rule(make_rule())

    result = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-1"))

    state = store.get_rule_state(rule.rule_id)
    assert sequence.call_count == 1
    assert (result.run.status, result.run.reason_code) == (
        "baseline_established",
        "baseline_established",
    )
    assert state.last_fingerprint
    assert state.next_reconcile_at == NOW + timedelta(seconds=300)
    assert store.list_outbox(rule.owner) == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_save_rule_maps_owner_conflict_without_replacing_original(tmp_path) -> None:
    coordinator, store, _, _ = make_harness(tmp_path, [{"event": "unused"}])
    original = coordinator.save_rule(make_rule())
    takeover = make_rule(
        owner=WakeOwner(
            tenant_id=original.owner.tenant_id,
            user_id="other-user",
            project_id=original.owner.project_id,
        )
    )

    with pytest.raises(ProactiveWakeError) as raised:
        coordinator.save_rule(takeover)

    assert raised.value.code == "rule_owner_conflict"
    assert store.get_rule(original.owner, original.rule_id) == original
    assert store.get_rule(takeover.owner, takeover.rule_id) is None


def test_save_rule_rejects_invalid_probe_arguments_without_persistence(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "unused"}])
    invalid = make_rule()
    invalid = invalid.model_copy(
        update={
            "probe": invalid.probe.model_copy(
                update={"arguments": {"unrelated": "Bearer secret-probe-input"}}
            )
        }
    )

    with pytest.raises(ProactiveWakeError) as raised:
        coordinator.save_rule(invalid)

    assert raised.value.code == "proactive_probe_arguments_invalid"
    assert "secret-probe-input" not in raised.value.message
    assert store.get_rule(invalid.owner, invalid.rule_id) is None
    assert store.list_rules(invalid.owner) == []
    assert sequence.call_count == 0


def test_runtime_schema_change_persists_config_error_without_probe_or_outbox(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "must-not-run"}])
    rule = coordinator.save_rule(make_rule())
    sequence.tool.input_schema = StrictQueryInput

    result = run_rule(coordinator, rule, make_signal(rule, signal_id="schema-changed"))

    assert (result.run.status, result.run.reason_code) == (
        "config_error",
        "proactive_probe_arguments_invalid",
    )
    assert sequence.call_count == 0
    assert store.list_outbox(rule.owner) == []
    persisted = next(
        run for run in store.list_runs(rule.owner) if run.signal_id == "schema-changed"
    )
    assert (persisted.status, persisted.reason_code) == (
        "config_error",
        "proactive_probe_arguments_invalid",
    )


def test_same_evidence_stays_silent_without_notification(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(
        tmp_path, [{"event": "baseline"}, {"event": "baseline"}]
    )
    rule = coordinator.save_rule(make_rule())
    first = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-1"))
    first_fingerprint = store.get_rule_state(rule.rule_id).last_fingerprint

    second = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-2"))

    assert sequence.call_count == 2
    assert first.run.status == "baseline_established"
    assert (second.run.status, second.run.reason_code) == ("unchanged", "unchanged")
    assert store.get_rule_state(rule.rule_id).last_fingerprint == first_fingerprint
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_changed_evidence_enqueues_exactly_one_notification(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(
        tmp_path,
        [{"event": "baseline"}, {"event": "baseline"}, {"event": "changed"}],
    )
    rule = coordinator.save_rule(make_rule())
    for index in range(2):
        run_rule(coordinator, rule, make_signal(rule, signal_id=f"signal-{index}"))

    result = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-3"))

    state = store.get_rule_state(rule.rule_id)
    outbox = store.list_outbox(rule.owner)
    assert sequence.call_count == 3
    assert (result.run.status, result.run.reason_code) == ("enqueued", "allowed")
    assert len(outbox) == 1
    assert result.notification == outbox[0]
    assert result.run.decision is None
    assert result.notification.message
    assert outbox[0].evidence_fingerprint == state.last_fingerprint
    assert state.notification_count == 1
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_duplicate_event_key_skips_probe(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "baseline"}])
    rule = coordinator.save_rule(make_rule())
    first = make_signal(rule, signal_id="signal-1", event_key="event-1")
    second = make_signal(rule, signal_id="signal-2", event_key="event-1")

    run_rule(coordinator, rule, first)
    result = run_rule(coordinator, rule, second)

    assert sequence.call_count == 1
    assert result.run.status == "deduplicated"
    assert result.run.reason_code is None
    assert store.get_rule_state(rule.rule_id).last_fingerprint
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_active_realtime_run_enqueues_deferred_notification(tmp_path) -> None:
    coordinator, store, sequence, activity = make_harness(
        tmp_path, [{"event": "initial"}], active=True
    )
    rule = coordinator.save_rule(make_rule(notify_on_initial=True))

    result = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-1"))

    outbox = store.list_outbox(rule.owner)
    assert sequence.call_count == 1
    assert activity.calls == [rule.owner]
    assert (result.run.status, result.run.reason_code) == ("enqueued", "active_conversation")
    assert len(outbox) == 1
    assert outbox[0].deliver_after == NOW + timedelta(seconds=60)
    assert store.get_rule_state(rule.rule_id).notification_count == 1
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_invalid_or_write_rule_becomes_config_error_without_mutating_enabled(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "initial"}])
    rule = coordinator.save_rule(make_rule())
    sequence.tool.execution = ToolExecutionPolicy(resource_writes=["calendar.events"])

    result = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-1"))

    assert sequence.call_count == 0
    assert (result.run.status, result.run.reason_code) == (
        "config_error",
        "proactive_tool_not_read_only",
    )
    assert store.get_rule_state(rule.rule_id).last_fingerprint is None
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_probe_failure_keeps_previous_fingerprint_and_creates_no_notification(tmp_path) -> None:
    failure = ToolResult(
        tool_name="calendar.search_events",
        success=False,
        error="provider_auth_failed",
        data={"raw": "private"},
    )
    coordinator, store, sequence, _ = make_harness(
        tmp_path, [{"event": "baseline"}, failure]
    )
    rule = coordinator.save_rule(make_rule())
    run_rule(coordinator, rule, make_signal(rule, signal_id="signal-1"))
    previous = store.get_rule_state(rule.rule_id).last_fingerprint

    result = run_rule(coordinator, rule, make_signal(rule, signal_id="signal-2"))

    assert sequence.call_count == 2
    assert (result.run.status, result.run.reason_code) == (
        "probe_failed",
        "provider_auth_failed",
    )
    assert store.get_rule_state(rule.rule_id).last_fingerprint == previous
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_missing_or_cross_user_rule_does_not_probe(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "baseline"}])
    rule = coordinator.save_rule(make_rule())
    wrong_owner = WakeOwner(user_id="other-user")

    for rule_id, owner in (("missing", rule.owner), (rule.rule_id, wrong_owner)):
        signal = make_signal(rule, signal_id=f"signal-{rule_id}", owner=owner)
        with pytest.raises(ProactiveWakeError) as raised:
            asyncio.run(coordinator.run_rule(rule_id=rule_id, owner=owner, signal=signal))
        assert raised.value.code == "rule_not_found"

    assert sequence.call_count == 0
    assert store.list_runs(rule.owner) == []
    assert store.get_rule_state(rule.rule_id).last_fingerprint is None
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_signal_owner_mismatch_does_not_probe(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "baseline"}])
    rule = coordinator.save_rule(make_rule())
    mismatched = make_signal(
        rule, signal_id="signal-1", owner=WakeOwner(user_id="other-user")
    )

    with pytest.raises(ProactiveWakeError) as raised:
        run_rule(coordinator, rule, mismatched)

    assert raised.value.code == "signal_owner_mismatch"
    assert sequence.call_count == 0
    assert store.list_runs(rule.owner) == []
    assert store.get_rule_state(rule.rule_id).last_fingerprint is None
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


@pytest.mark.parametrize(
    ("source", "event_type"),
    [("mail", "calendar.changed"), ("calendar", "calendar.deleted")],
    ids=["source", "type"],
)
def test_provider_event_source_or_type_mismatch_does_not_probe(
    tmp_path, source, event_type
) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "baseline"}])
    rule = coordinator.save_rule(make_rule())
    signal = make_signal(
        rule,
        signal_id="signal-1",
        source=source,
        event_type=event_type,
    )

    with pytest.raises(ProactiveWakeError) as raised:
        run_rule(coordinator, rule, signal)

    assert raised.value.code == "signal_not_matched"
    assert sequence.call_count == 0
    assert store.list_runs(rule.owner) == []
    assert store.get_rule_state(rule.rule_id).last_fingerprint is None
    assert store.list_outbox() == []
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_persisted_run_and_sqlite_file_exclude_raw_tool_payload(tmp_path) -> None:
    secret = "raw-private-calendar-token"
    result = ToolResult(
        tool_name="calendar.search_events",
        success=True,
        data={"raw": secret},
        model_observation={"event": "safe"},
        raw_data_ref=f"calendar-raw://{secret}",
    )
    coordinator, store, sequence, _ = make_harness(tmp_path, [result])
    rule = coordinator.save_rule(make_rule(notify_on_initial=True))

    run_rule(coordinator, rule, make_signal(rule, signal_id="signal-1"))

    loaded_run = store.list_runs(rule.owner)[0]
    outbox = store.list_outbox(rule.owner)
    assert sequence.call_count == 1
    assert loaded_run.status == "enqueued"
    assert loaded_run.reason_code == "allowed"
    assert secret not in loaded_run.model_dump_json()
    assert secret not in "".join(item.model_dump_json() for item in outbox)
    assert secret.encode() not in store.path.read_bytes()
    assert len(outbox) == 1
    assert store.get_rule_state(rule.rule_id).last_fingerprint
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_concurrent_distinct_signals_for_same_rule_are_serialized(tmp_path) -> None:
    payload = {"event": "same"}
    coordinator, store, sequence, _ = make_harness(
        tmp_path, [payload, payload], delay_s=0.05
    )
    rule = coordinator.save_rule(make_rule(notify_on_initial=True))
    first = make_signal(rule, signal_id="signal-1")
    second = make_signal(rule, signal_id="signal-2")

    async def run_concurrently():
        first_task = asyncio.create_task(
            coordinator.run_rule(rule_id=rule.rule_id, owner=rule.owner, signal=first)
        )
        await asyncio.sleep(0)
        second_task = asyncio.create_task(
            coordinator.run_rule(rule_id=rule.rule_id, owner=rule.owner, signal=second)
        )
        return await asyncio.gather(first_task, second_task)

    results = asyncio.run(run_concurrently())

    state = store.get_rule_state(rule.rule_id)
    assert sequence.call_count == 2
    assert sequence.max_active == 1
    assert [item.run.status for item in results] == ["enqueued", "unchanged"]
    assert state.last_fingerprint == results[1].run.evidence.fingerprint
    assert len(store.list_outbox(rule.owner)) == 1
    assert state.notification_count == 1
    assert store.get_rule(rule.owner, rule.rule_id).enabled is True


def test_coordinator_instances_share_process_local_rule_lock(tmp_path) -> None:
    payload = {"event": "same"}
    coordinator, store, sequence, activity = make_harness(
        tmp_path, [payload, payload], delay_s=0.05
    )
    other = ProactiveWakeCoordinator(
        store=store,
        rule_validator=coordinator.rule_validator,
        probe_runner=coordinator.probe_runner,
        activity_reader=activity,
        now_fn=lambda: NOW,
    )
    rule = coordinator.save_rule(make_rule(notify_on_initial=True))

    async def run_concurrently():
        return await asyncio.gather(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="signal-1"),
            ),
            other.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="signal-2"),
            ),
        )

    results = asyncio.run(run_concurrently())

    assert sequence.call_count == 2
    assert sequence.max_active == 1
    assert [item.run.status for item in results] == ["enqueued", "unchanged"]
    assert len(store.list_outbox(rule.owner)) == 1
    assert store.get_rule_state(rule.rule_id).notification_count == 1


def test_cancellation_waits_for_probe_thread_before_releasing_rule_lock(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(
        tmp_path, [{"event": "first"}, {"event": "second"}], delay_s=0.1
    )
    rule = coordinator.save_rule(make_rule())

    async def cancel_then_run_again():
        first = asyncio.create_task(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="signal-1"),
            )
        )
        while sequence.active == 0:
            await asyncio.sleep(0.005)
        first.cancel()
        second = asyncio.create_task(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="signal-2"),
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await first
        return await second

    second = asyncio.run(cancel_then_run_again())

    assert sequence.call_count == 2
    assert sequence.max_active == 1
    assert second.run.status == "baseline_established"
    assert store.list_outbox(rule.owner) == []
    cancelled_run = next(
        item for item in store.list_runs(rule.owner) if item.signal_id == "signal-1"
    )
    assert (cancelled_run.status, cancelled_run.reason_code) == (
        "probe_failed",
        "proactive_run_cancelled",
    )


def test_coordinator_serializes_same_rule_across_event_loops_and_threads(tmp_path) -> None:
    payload = {"event": "same"}
    coordinator, store, sequence, activity = make_harness(
        tmp_path, [payload, payload], delay_s=0.1
    )
    other = ProactiveWakeCoordinator(
        store=store,
        rule_validator=coordinator.rule_validator,
        probe_runner=coordinator.probe_runner,
        activity_reader=activity,
        now_fn=lambda: NOW,
    )
    rule = coordinator.save_rule(make_rule(notify_on_initial=True))
    barrier = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def invoke(target, signal_id):
        try:
            barrier.wait()
            results.append(
                asyncio.run(
                    target.run_rule(
                        rule_id=rule.rule_id,
                        owner=rule.owner,
                        signal=make_signal(rule, signal_id=signal_id),
                    )
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, args=(coordinator, "signal-thread-1")),
        threading.Thread(target=invoke, args=(other, "signal-thread-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sequence.call_count == 2
    assert sequence.max_active == 1
    assert sorted(item.run.status for item in results) == ["enqueued", "unchanged"]
    assert len(store.list_outbox(rule.owner)) == 1


def test_cancelled_cross_loop_lock_waiter_does_not_leak_process_lock(tmp_path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    sequence = SequenceTool([{"event": "same"}] * 3)
    original_handler = sequence.tool._handler

    def blocking_first(input, context):
        if sequence.call_count == 0:
            first_started.set()
            release_first.wait(timeout=30)
        return original_handler(input, context)

    sequence.tool._handler = blocking_first
    registry = ToolRegistry()
    registry.register(sequence.tool)
    validator = ProactiveRuleValidator(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )
    runner = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    first_coordinator = ProactiveWakeCoordinator(
        store=store,
        rule_validator=validator,
        probe_runner=runner,
        now_fn=lambda: NOW,
    )
    waiter_coordinator = ProactiveWakeCoordinator(
        store=store,
        rule_validator=validator,
        probe_runner=runner,
        now_fn=lambda: NOW,
    )
    rule = first_coordinator.save_rule(make_rule())
    first_error: list[BaseException] = []

    def run_first():
        try:
            asyncio.run(
                first_coordinator.run_rule(
                    rule_id=rule.rule_id,
                    owner=rule.owner,
                    signal=make_signal(rule, signal_id="signal-holder"),
                )
            )
        except BaseException as exc:
            first_error.append(exc)

    holder = threading.Thread(target=run_first)
    holder.start()
    assert first_started.wait(timeout=30)

    async def cancel_waiter_then_run_third():
        waiter = asyncio.create_task(
            waiter_coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="signal-waiter"),
            )
        )
        await asyncio.sleep(0.05)
        waiter.cancel()
        threading.Timer(0.05, release_first.set).start()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        return await asyncio.wait_for(
            waiter_coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="signal-third"),
            ),
            timeout=30,
        )

    third = asyncio.run(cancel_waiter_then_run_third())
    holder.join(timeout=30)

    assert first_error == []
    assert not holder.is_alive()
    assert third.run.status == "unchanged"
    assert all(item.signal_id != "signal-waiter" for item in store.list_runs(rule.owner))


class RaisingActivityReader:
    async def is_active(self, owner):
        raise RuntimeError("activity exploded")


class RaisingEvaluator:
    def evaluate(self, **kwargs):
        raise RuntimeError("evaluator exploded")


class RaisingAttentionPolicy:
    def evaluate(self, **kwargs):
        raise RuntimeError("attention exploded")


class InvalidEnvelopeEvaluator:
    def evaluate(self, *, evidence, **kwargs):
        return WakeDecision.model_construct(
            outcome="notify",
            severity="normal",
            reason_code="forced_notify",
            summary="Safe summary",
            user_message=None,
            evidence_ids=[evidence.evidence_id],
            confidence=None,
            expires_at=NOW + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    ("stage", "reason_code", "expected_exception"),
    [
        ("probe", "proactive_probe_failed", RuntimeError),
        ("activity", "proactive_activity_failed", RuntimeError),
        ("evaluator", "proactive_evaluator_failed", RuntimeError),
        ("attention", "proactive_attention_failed", RuntimeError),
        ("envelope", "proactive_envelope_failed", ValueError),
    ],
)
def test_unexpected_pipeline_exception_terminalizes_claimed_run(
    tmp_path, stage, reason_code, expected_exception
) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "initial"}])
    rule = coordinator.save_rule(make_rule(notify_on_initial=True))
    if stage == "probe":
        coordinator.probe_runner.run = lambda rule, signal: (_ for _ in ()).throw(
            RuntimeError("probe exploded")
        )
    elif stage == "activity":
        coordinator.activity_reader = RaisingActivityReader()
    elif stage == "evaluator":
        coordinator.evaluator = RaisingEvaluator()
    elif stage == "attention":
        coordinator.attention_policy = RaisingAttentionPolicy()
    else:
        coordinator.evaluator = InvalidEnvelopeEvaluator()

    with pytest.raises(expected_exception):
        run_rule(coordinator, rule, make_signal(rule, signal_id=f"signal-{stage}"))

    loaded = next(
        item for item in store.list_runs(rule.owner) if item.signal_id == f"signal-{stage}"
    )
    assert (loaded.status, loaded.reason_code) == ("probe_failed", reason_code)
    assert store.get_rule_state(rule.rule_id).last_fingerprint is None
    assert store.list_outbox(rule.owner) == []
    assert sequence.call_count == (0 if stage == "probe" else 1)


def test_process_lock_waiters_do_not_exhaust_default_executor() -> None:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    task_count = DEFAULT_EXECUTOR_WORKERS + 2
    process = context.Process(
        target=run_executor_saturation_scenario,
        args=(task_count, result_queue),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("same-rule waiters exhausted the default executor")

    assert process.exitcode == 0
    result = result_queue.get(timeout=5)
    assert "error" not in result
    assert result["call_count"] == 1
    assert result["max_active"] == 1
    assert result["registry_clean"] is True


def test_state_read_failure_terminalizes_claimed_run_with_fallback_state(tmp_path) -> None:
    coordinator, store, sequence, _ = make_harness(tmp_path, [{"event": "unused"}])
    rule = coordinator.save_rule(make_rule())
    original_get_state = store.get_rule_state
    failed_once = False

    def fail_once(rule_id):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("state read exploded")
        return original_get_state(rule_id)

    store.get_rule_state = fail_once

    with pytest.raises(RuntimeError, match="state read exploded"):
        run_rule(coordinator, rule, make_signal(rule, signal_id="state-read-failure"))

    loaded = next(
        item for item in store.list_runs(rule.owner) if item.signal_id == "state-read-failure"
    )
    assert (loaded.status, loaded.reason_code) == (
        "probe_failed",
        "proactive_state_read_failed",
    )
    assert sequence.call_count == 0
    assert original_get_state(rule.rule_id) == WakeRuleState(rule_id=rule.rule_id)
    assert store.list_outbox(rule.owner) == []


@pytest.mark.parametrize("terminal_failure", ["now", "store"])
def test_terminalization_failure_preserves_original_exception(
    tmp_path, terminal_failure
) -> None:
    coordinator, store, _, _ = make_harness(tmp_path, [{"event": "unused"}])
    rule = coordinator.save_rule(make_rule())
    original_probe_error = RuntimeError("original probe exploded")
    coordinator.probe_runner.run = lambda rule, signal: (_ for _ in ()).throw(
        original_probe_error
    )
    if terminal_failure == "now":
        now_calls = 0

        def failing_terminal_now():
            nonlocal now_calls
            now_calls += 1
            if now_calls > 1:
                raise RuntimeError("terminal clock exploded")
            return NOW

        coordinator.now_fn = failing_terminal_now
    else:
        original_complete = store.complete_outcome

        def failing_terminal_complete(*, run, state, notification):
            if run.status == "probe_failed":
                raise RuntimeError("terminal store exploded")
            return original_complete(run=run, state=state, notification=notification)

        store.complete_outcome = failing_terminal_complete

    with pytest.raises(RuntimeError) as raised:
        run_rule(coordinator, rule, make_signal(rule, signal_id=f"terminal-{terminal_failure}"))

    assert raised.value is original_probe_error
    loaded = next(
        item
        for item in store.list_runs(rule.owner)
        if item.signal_id == f"terminal-{terminal_failure}"
    )
    assert loaded.status == "probing"


def test_cancelled_run_preserves_cancel_when_probe_finishes_with_exception(tmp_path) -> None:
    coordinator, store, _, _ = make_harness(tmp_path, [{"event": "unused"}])
    rule = coordinator.save_rule(make_rule())
    probe_started = threading.Event()
    release_probe = threading.Event()

    def blocking_failed_probe(rule, signal):
        probe_started.set()
        release_probe.wait(timeout=30)
        raise RuntimeError("probe failed after cancellation")

    coordinator.probe_runner.run = blocking_failed_probe

    async def scenario():
        task = asyncio.create_task(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal=make_signal(rule, signal_id="cancel-then-probe-fails"),
            )
        )
        while not probe_started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        release_probe.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    loaded = next(
        item
        for item in store.list_runs(rule.owner)
        if item.signal_id == "cancel-then-probe-fails"
    )
    assert (loaded.status, loaded.reason_code) == (
        "probe_failed",
        "proactive_run_cancelled",
    )


def test_process_rule_lock_registry_cleans_last_user(tmp_path) -> None:
    coordinator, _, _, _ = make_harness(tmp_path, [{"event": "baseline"}])
    rule = coordinator.save_rule(make_rule(rule_id="registry-cleanup-rule"))
    key = coordinator_module._lock_key(rule.owner, rule.rule_id)

    run_rule(coordinator, rule, make_signal(rule, signal_id="registry-cleanup"))

    assert key not in coordinator_module._PROCESS_RULE_LOCKS
