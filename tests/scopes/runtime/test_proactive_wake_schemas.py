from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.proactive_wake import (
    WakeConditionSpec,
    WakeDecision,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeTriggerSpec,
)


def test_wake_owner_persists_only_stable_identity() -> None:
    identity = RequestIdentity.for_user(
        user_id="u1",
        session_id="temporary-session",
        tenant_id="tenant-1",
        project_id="project-1",
        allowed_scopes=["session", "user_profile"],
    )

    owner = WakeOwner.from_identity(identity)

    assert owner.model_dump() == {
        "tenant_id": "tenant-1",
        "user_id": "u1",
        "project_id": "project-1",
    }


def test_changed_rule_defaults_to_silent_initial_baseline() -> None:
    rule = WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(user_id="u1"),
        name="Calendar changes",
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(tool_name="calendar.search_events", arguments={"query": "next two hours"}),
        condition=WakeConditionSpec(mode="changed", notify_when="Calendar evidence changes"),
    )

    assert rule.condition.notify_on_initial is False
    assert rule.version == 1
    assert rule.enabled is True


def test_silent_decision_rejects_user_message() -> None:
    with pytest.raises(ValidationError):
        WakeDecision(
            outcome="silent",
            severity="normal",
            reason_code="unchanged",
            summary="No change.",
            user_message="This must not be sent.",
            evidence_ids=["e1"],
        )


def test_notify_decision_requires_message_and_evidence() -> None:
    with pytest.raises(ValidationError):
        WakeDecision(
            outcome="notify",
            severity="normal",
            reason_code="evidence_changed",
            summary="Changed.",
            evidence_ids=[],
        )
