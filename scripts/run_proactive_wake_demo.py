"""Run the deterministic offline Proactive Wake Phase 1 demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.schemas.tools import (  # noqa: E402
    ApprovalPolicy,
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
)
from assistant_agent.services.proactive_wake import (  # noqa: E402
    GovernedProbeRunner,
    MockProactiveNotificationTransport,
    NotificationDeliveryWorker,
    NullUserActivityReader,
    ProactiveRuleValidator,
    ProactiveWakeCoordinator,
    SQLiteProactiveWakeStore,
    WakeAttentionSpec,
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.tools.decorators import tool  # noqa: E402
from assistant_agent.tools.registry import ToolRegistry  # noqa: E402


DEMO_NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
CALENDAR_TOOL_NAME = "calendar.search_events"


class CalendarSearchInput(BaseModel):
    query: str = Field(min_length=1)


class OfflineCalendarSequence:
    """One in-process read-only calendar tool with two deterministic results."""

    def __init__(self) -> None:
        self.call_count = 0
        self._observations = [
            {
                "events": [
                    {
                        "event_id": "demo-meeting",
                        "starts_at": "2026-07-14T10:00:00+00:00",
                        "title": "Project check-in",
                    }
                ]
            },
            {
                "events": [
                    {
                        "event_id": "demo-meeting",
                        "starts_at": "2026-07-14T11:00:00+00:00",
                        "title": "Project check-in",
                    }
                ]
            },
        ]

        @tool(
            name=CALENDAR_TOOL_NAME,
            description="Search deterministic offline calendar events.",
            input_schema=CalendarSearchInput,
            execution=ToolExecutionPolicy(
                dependency_mode="independent",
                resource_reads=["calendar.events"],
                resource_writes=[],
                realtime_safety="safe",
                artifact_reuse="reusable",
            ),
            policy=ToolPolicyMetadata(
                risk="external_read",
                approval=ApprovalPolicy(mode="never"),
            ),
        )
        def calendar_search_events(input, context):
            del input, context
            index = min(self.call_count, len(self._observations) - 1)
            self.call_count += 1
            observation = self._observations[index]
            return ToolResult(
                tool_name=CALENDAR_TOOL_NAME,
                success=True,
                data={"offline": True, "event_count": len(observation["events"])},
                model_observation=observation,
                trace_summary={"offline": True, "event_count": len(observation["events"])},
            )

        self.tool = calendar_search_events


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic offline Proactive Wake Phase 1 demo."
    )
    parser.add_argument("--db", required=True, help="SQLite database path for this demo run.")
    return parser


async def _run_demo(db_path: Path) -> dict[str, Any]:
    calendar = OfflineCalendarSequence()
    registry = ToolRegistry()
    registry.register(calendar.tool)
    allowed_tools = {CALENDAR_TOOL_NAME}
    rule_validator = ProactiveRuleValidator(
        registry=registry,
        allowed_tool_names=allowed_tools,
    )
    probe_runner = GovernedProbeRunner(
        registry=registry,
        allowed_tool_names=allowed_tools,
    )
    store = SQLiteProactiveWakeStore(db_path)
    activity_reader = NullUserActivityReader()
    coordinator = ProactiveWakeCoordinator(
        store=store,
        rule_validator=rule_validator,
        probe_runner=probe_runner,
        activity_reader=activity_reader,
        now_fn=lambda: DEMO_NOW,
    )
    rule = coordinator.save_rule(
        WakeRule(
            rule_id="proactive-wake-offline-demo",
            owner=WakeOwner(user_id="offline-demo-user"),
            name="Offline calendar change demo",
            trigger=WakeTriggerSpec(
                event_sources=["calendar"],
                event_types=["calendar.changed"],
                reconcile_interval_s=300,
            ),
            probe=WakeProbeSpec(
                tool_name=CALENDAR_TOOL_NAME,
                arguments={"query": "next two hours"},
            ),
            condition=WakeConditionSpec(
                mode="changed",
                notify_when="The scheduled calendar event time changes.",
            ),
            attention=WakeAttentionSpec(cooldown_s=0),
            created_at=DEMO_NOW,
            updated_at=DEMO_NOW,
        )
    )

    baseline = await coordinator.run_rule(
        rule_id=rule.rule_id,
        owner=rule.owner,
        signal=WakeSignal(
            signal_id="offline-demo-baseline",
            kind="manual",
            source="offline_demo",
            event_type="manual.baseline",
            occurred_at=DEMO_NOW,
            owner=rule.owner,
            event_key="offline-demo-baseline",
        ),
    )
    changed = await coordinator.run_rule(
        rule_id=rule.rule_id,
        owner=rule.owner,
        signal=WakeSignal(
            signal_id="offline-demo-change",
            kind="manual",
            source="offline_demo",
            event_type="manual.change",
            occurred_at=DEMO_NOW,
            owner=rule.owner,
            event_key="offline-demo-change",
        ),
    )

    delivery_worker = NotificationDeliveryWorker(
        store=store,
        transport=MockProactiveNotificationTransport(),
        activity_reader=activity_reader,
        now_fn=lambda: DEMO_NOW,
    )
    delivered = await delivery_worker.drain_once()
    return {
        "offline": True,
        "llm_calls": 0,
        "probe_calls": calendar.call_count,
        "baseline_status": baseline.run.status,
        "changed_status": changed.run.status,
        "delivered_count": len(delivered),
        "delivery_status": delivered[0].status if delivered else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = asyncio.run(_run_demo(Path(args.db)))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
