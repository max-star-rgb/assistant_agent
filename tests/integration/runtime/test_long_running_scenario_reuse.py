"""Verify new monitoring scenarios reuse ProactiveWake without core changes."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from assistant_agent.schemas.proactive_wake import (
    WakeAttentionSpec,
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.proactive_wake.coordinator import (
    ProactiveWakeCoordinator,
)
from assistant_agent.services.proactive_wake.probe import (
    GovernedProbeRunner,
    ProactiveRuleValidator,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class _RouteProbeInput(BaseModel):
    route_id: str = Field(min_length=1)


class _MailboxProbeInput(BaseModel):
    query: str = Field(min_length=1)


class _CommuteProbeTool(ToolBase):
    name = "commute_status_probe"
    description = "Read one configured commute route status."
    input_schema = _RouteProbeInput
    output_schema = ToolResult
    category = "read"

    def __init__(self) -> None:
        self._statuses = ["normal", "line_suspended", "line_suspended"]
        self.run_count = 0

    def _run(
        self,
        input: _RouteProbeInput,
        context: ToolContext,
    ) -> ToolResult:
        self.run_count += 1
        status = self._statuses.pop(0)
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"route_id": input.route_id, "status": status},
            model_observation={
                "summary": f"Route {input.route_id} status is {status}.",
                "route_id": input.route_id,
                "status": status,
            },
            output_ref=f"mock://commute/{input.route_id}/{status}",
        )


class _EmailCommitmentProbeTool(ToolBase):
    name = "email_commitment_probe"
    description = "Read the count of matching commitment emails."
    input_schema = _MailboxProbeInput
    output_schema = ToolResult
    category = "read"

    def __init__(self) -> None:
        self._counts = [0, 1, 1]
        self.run_count = 0

    def _run(
        self,
        input: _MailboxProbeInput,
        context: ToolContext,
    ) -> ToolResult:
        self.run_count += 1
        count = self._counts.pop(0)
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"query": input.query, "matching_count": count},
            model_observation={
                "summary": f"{count} matching commitment email(s) observed.",
                "query": input.query,
                "matching_count": count,
            },
            output_ref=f"mock://email-commitments/{count}",
        )


def _rule(
    *,
    owner: WakeOwner,
    rule_id: str,
    name: str,
    tool_name: str,
    arguments: dict,
) -> WakeRule:
    return WakeRule(
        rule_id=rule_id,
        owner=owner,
        name=name,
        trigger=WakeTriggerSpec(reconcile_interval_s=60),
        probe=WakeProbeSpec(tool_name=tool_name, arguments=arguments),
        condition=WakeConditionSpec(
            mode="changed",
            notify_when="the monitored evidence changes",
            notify_on_initial=False,
        ),
        attention=WakeAttentionSpec(cooldown_s=0),
    )


def _signal(owner: WakeOwner, event_type: str) -> WakeSignal:
    return WakeSignal(
        kind="manual",
        source="scenario_reuse_test",
        event_type=event_type,
        owner=owner,
    )


def test_commute_and_email_monitors_reuse_governed_wake_and_outbox(
    tmp_path,
) -> None:
    owner = WakeOwner(user_id="scenario-user", agent_id="agent_default")
    commute_tool = _CommuteProbeTool()
    email_tool = _EmailCommitmentProbeTool()
    registry = ToolRegistry()
    registry.register(commute_tool)
    registry.register(email_tool)
    allowed = {commute_tool.name, email_tool.name}
    validator = ProactiveRuleValidator(
        registry=registry,
        allowed_tool_names=allowed,
    )
    store = SQLiteProactiveWakeStore(tmp_path / "scenario-reuse.sqlite3")
    coordinator = ProactiveWakeCoordinator(
        store=store,
        rule_validator=validator,
        probe_runner=GovernedProbeRunner(
            registry=registry,
            allowed_tool_names=allowed,
        ),
    )
    rules = [
        _rule(
            owner=owner,
            rule_id="commute-disruption-watch",
            name="通勤线路异常",
            tool_name=commute_tool.name,
            arguments={"route_id": "metro-line-2"},
        ),
        _rule(
            owner=owner,
            rule_id="email-commitment-watch",
            name="邮件承诺变化",
            tool_name=email_tool.name,
            arguments={"query": "承诺 OR deadline"},
        ),
    ]

    for rule in rules:
        coordinator.save_rule(rule)
        baseline = asyncio.run(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=owner,
                signal=_signal(owner, "baseline"),
            )
        )
        assert baseline.notification is None
        assert baseline.run.status == "baseline_established"

        changed = asyncio.run(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=owner,
                signal=_signal(owner, "changed"),
            )
        )
        assert changed.notification is not None
        assert changed.run.status == "enqueued"
        assert changed.notification.rule_id == rule.rule_id

        unchanged = asyncio.run(
            coordinator.run_rule(
                rule_id=rule.rule_id,
                owner=owner,
                signal=_signal(owner, "unchanged"),
            )
        )
        assert unchanged.notification is None
        assert unchanged.run.status == "unchanged"

    notifications = store.list_outbox()
    assert {item.rule_id for item in notifications} == {
        "commute-disruption-watch",
        "email-commitment-watch",
    }
    assert len(notifications) == 2
    assert commute_tool.run_count == 3
    assert email_tool.run_count == 3
