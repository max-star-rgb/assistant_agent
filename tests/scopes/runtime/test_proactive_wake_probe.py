from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, Field

from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.proactive_wake import (
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeRuleState,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.schemas.tools import (
    ApprovalPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
)
from assistant_agent.services.proactive_wake.probe import (
    GovernedProbeRunner,
    ProbeObservation,
    ProactiveRuleValidator,
)
from assistant_agent.services.proactive_wake.change_detector import (
    build_wake_evidence,
    evidence_fingerprint,
)
from assistant_agent.tools.decorators import tool
from assistant_agent.tools.registry import ToolRegistry


class QueryInput(BaseModel):
    query: str = Field(min_length=1)


def make_rule(
    *,
    tool_name: str = "calendar.search_events",
    arguments: dict[str, Any] | None = None,
    condition_mode: str = "changed",
    enabled: bool = True,
) -> WakeRule:
    return WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(tenant_id="tenant-1", user_id="user-1", project_id="project-1"),
        name="Calendar changes",
        enabled=enabled,
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(
            tool_name=tool_name,
            arguments=arguments if arguments is not None else {"query": "next two hours"},
        ),
        condition=WakeConditionSpec(
            mode=condition_mode,
            notify_when="Calendar evidence changes",
        ),
    )


def make_signal(rule: WakeRule) -> WakeSignal:
    return WakeSignal(
        signal_id="signal-1",
        kind="provider_event",
        source="calendar",
        event_type="calendar.changed",
        event_key="calendar-event-1",
        owner=rule.owner,
    )


def make_calendar_tool(calls: list[dict[str, Any]]):
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
        calls.append(
            {
                "query": input.query,
                "user_id": context.user_id,
                "session_id": context.session_id,
                "metadata": context.metadata,
            }
        )
        return ToolResult(
            tool_name="calendar.search_events",
            success=True,
            data={
                "provider_raw_response": {"secret": "provider-secret"},
                "summary": "Unsafe provider response.",
            },
            model_observation={
                "summary": "Calendar search returned 1 event.",
                "events": [{"title": "Product sync", "time": "10:00"}],
            },
            audit_payload={"provider": "mock_calendar", "token": "audit-secret"},
            raw_data_ref="calendar-raw://events/today",
            output_ref="calendar-event://event-1",
        )

    return calendar_search_events


def make_write_tool(calls: list[str]):
    @tool(
        name="calendar.create_event",
        description="Create a calendar event.",
        input_schema=QueryInput,
        execution=ToolExecutionPolicy(
            dependency_mode="independent",
            resource_writes=["calendar.events"],
        ),
        policy=ToolPolicyMetadata(
            risk="external_write",
            approval=ApprovalPolicy(mode="never"),
        ),
    )
    def calendar_create_event(input, context):
        calls.append(input.query)
        return {"summary": "Event created."}

    return calendar_create_event


def test_read_only_allowlisted_probe_runs_through_validator_and_executor() -> None:
    calls: list[dict[str, Any]] = []
    registry = ToolRegistry()
    registry.register(make_calendar_tool(calls))
    rule = make_rule()
    validator = ProactiveRuleValidator(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )
    runner = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )

    validation = validator.validate(rule)
    observation = runner.run(rule, make_signal(rule))

    assert validation.accepted is True
    assert observation.accepted is True
    assert observation.code == "succeeded"
    assert observation.success is True
    assert observation.summary == "Calendar search returned 1 event."
    assert observation.prompt_safe_payload == {
        "summary": "Calendar search returned 1 event.",
        "events": [{"title": "Product sync", "time": "10:00"}],
    }
    assert observation.source_refs == ["calendar-event://event-1"]
    assert calls == [
        {
            "query": "next two hours",
            "user_id": "user-1",
            "session_id": "proactive:rule-1",
            "metadata": {
                "request_text": "Explicit proactive wake rule probe.",
                "request_metadata": {
                    "source": "proactive_wake",
                    "tenant_id": "tenant-1",
                    "project_id": "project-1",
                    "rule_id": "rule-1",
                    "signal_id": "signal-1",
                },
            },
        }
    ]
    serialized = observation.model_dump_json()
    assert "calendar-raw://events/today" not in serialized
    assert "provider-secret" not in serialized
    assert "audit-secret" not in serialized


def test_probe_rejects_tool_not_in_explicit_allowlist() -> None:
    registry = ToolRegistry()
    registry.register(make_calendar_tool([]))

    validation = ProactiveRuleValidator(registry=registry, allowed_tool_names=set()).validate(make_rule())

    assert validation.accepted is False
    assert validation.code == "proactive_tool_not_allowed"


def test_probe_rejects_write_or_confirmation_tool_before_execution() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(make_write_tool(calls))
    rule = make_rule(tool_name="calendar.create_event")
    runner = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names={"calendar.create_event"},
    )

    observation = runner.run(rule, make_signal(rule))

    assert observation.accepted is False
    assert observation.code == "proactive_tool_not_read_only"
    assert observation.success is False
    assert calls == []


def test_probe_rejects_semantic_condition_in_phase_one() -> None:
    validation = ProactiveRuleValidator(
        registry=ToolRegistry(),
        allowed_tool_names={"calendar.search_events"},
    ).validate(make_rule(condition_mode="semantic"))

    assert validation.accepted is False
    assert validation.code == "proactive_condition_mode_unsupported"


def test_fingerprint_is_stable_for_key_order_and_changes_for_evidence() -> None:
    first = {
        "events": [{"title": "Product sync", "time": "10:00"}],
        "summary": "One event",
    }
    reordered = {
        "summary": "One event",
        "events": [{"time": "10:00", "title": "Product sync"}],
    }
    changed = {
        "events": [{"title": "Planning review", "time": "11:00"}],
        "summary": "One event",
    }

    assert evidence_fingerprint(first) == evidence_fingerprint(reordered)
    assert evidence_fingerprint(first) != evidence_fingerprint(changed)


def test_disabled_rule_returns_structured_validation_code() -> None:
    validator = ProactiveRuleValidator(
        registry=ToolRegistry(),
        allowed_tool_names={"calendar.search_events"},
    )

    disabled = validator.validate(make_rule(enabled=False))

    assert (disabled.accepted, disabled.code) == (False, "proactive_rule_disabled")


def test_unknown_rule_tool_returns_structured_validation_code() -> None:
    validator = ProactiveRuleValidator(
        registry=ToolRegistry(),
        allowed_tool_names={"calendar.search_events"},
    )

    unknown = validator.validate(make_rule())

    assert (unknown.accepted, unknown.code) == (False, "proactive_tool_unknown")


def test_invalid_probe_arguments_are_rejected_before_action_execution() -> None:
    calls: list[dict[str, Any]] = []
    registry = ToolRegistry()
    registry.register(make_calendar_tool(calls))
    rule = make_rule(arguments={"unrelated": "Bearer secret-probe-input"})
    validator = ProactiveRuleValidator(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    )

    validation = validator.validate(rule)
    observation = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    ).run(rule, make_signal(rule))

    assert validation.accepted is False
    assert validation.code == "proactive_probe_arguments_invalid"
    assert "secret-probe-input" not in validation.message
    assert observation.accepted is False
    assert observation.code == "proactive_probe_arguments_invalid"
    assert observation.success is False
    assert observation.prompt_safe_payload == {}
    assert observation.source_refs == []
    assert "secret-probe-input" not in observation.summary
    assert calls == []


def test_failed_tool_result_returns_only_prompt_safe_failure() -> None:
    registry = ToolRegistry()

    @tool(
        name="calendar.search_events",
        input_schema=QueryInput,
        execution=ToolExecutionPolicy(dependency_mode="independent"),
        policy=ToolPolicyMetadata(
            risk="external_read",
            approval=ApprovalPolicy(mode="never"),
        ),
    )
    def failing_calendar_search(input, context):
        return ToolResult(
            tool_name="calendar.search_events",
            success=False,
            data={"provider_raw_response": {"token": "provider-secret"}},
            error="provider_timeout: Bearer secret-token",
            audit_payload={"token": "audit-secret"},
            raw_data_ref="calendar-raw://failed",
        )

    registry.register(failing_calendar_search)
    rule = make_rule()

    observation = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names={"calendar.search_events"},
    ).run(rule, make_signal(rule))

    assert observation.accepted is True
    assert observation.code == "provider_timeout"
    assert observation.success is False
    serialized = observation.model_dump_json()
    assert "calendar-raw://failed" not in serialized
    assert "provider-secret" not in serialized
    assert "audit-secret" not in serialized
    assert "secret-token" not in serialized


def test_runner_rejects_executor_bound_to_different_registry() -> None:
    runner_registry = ToolRegistry()
    other_registry = ToolRegistry()

    with pytest.raises(ValueError, match="same registry"):
        GovernedProbeRunner(
            registry=runner_registry,
            allowed_tool_names=set(),
            tool_executor=ToolExecutor(registry=other_registry),
        )


def test_build_wake_evidence_tracks_initial_and_changed_fingerprints() -> None:
    observed_at = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    rule = make_rule()
    initial_observation = ProbeObservation(
        accepted=True,
        code="succeeded",
        tool_name="calendar.search_events",
        success=True,
        summary="One calendar event.",
        prompt_safe_payload={"events": [{"title": "Product sync", "time": "10:00"}]},
        source_refs=["calendar-event://event-1"],
    )
    initial = build_wake_evidence(
        rule=rule,
        observation=initial_observation,
        state=WakeRuleState(rule_id=rule.rule_id),
        observed_at=observed_at,
    )
    changed_observation = initial_observation.model_copy(
        update={
            "summary": "Calendar event moved.",
            "prompt_safe_payload": {"events": [{"title": "Product sync", "time": "11:00"}]},
        }
    )

    changed = build_wake_evidence(
        rule=rule,
        observation=changed_observation,
        state=WakeRuleState(rule_id=rule.rule_id, last_fingerprint=initial.fingerprint),
        observed_at=observed_at,
    )

    assert initial.is_initial is True
    assert initial.changed is False
    assert initial.previous_fingerprint is None
    assert initial.status == "succeeded"
    assert initial.source_refs == ["calendar-event://event-1"]
    assert changed.is_initial is False
    assert changed.changed is True
    assert changed.previous_fingerprint == initial.fingerprint
