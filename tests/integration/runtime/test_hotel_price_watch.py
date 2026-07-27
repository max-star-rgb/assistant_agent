"""Restart-safe hotel price watch vertical scenario."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.automation.durable_tasks.models import utc_now
from assistant_agent.identity import RequestIdentity
from assistant_agent.tools.plugins.builtin.lodging.models import (
    HotelPriceWatchGoal,
    LodgingSearchRequest,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.automation.durable_tasks.hotel_price_watch import (
    HotelPriceWatchRuntime,
    HotelPriceWatchService,
)
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.automation.durable_tasks.store import InMemoryTaskStore
from assistant_agent.automation.durable_tasks.worker import (
    DurableTaskRuntimeRouter,
    DurableTaskWorker,
)
from assistant_agent.automation.proactive_wake.delivery import (
    MockProactiveNotificationTransport,
    NotificationDeliveryWorker,
)
from assistant_agent.automation.proactive_wake.store import SQLiteProactiveWakeStore
from assistant_agent.tools.plugins.builtin.lodging import (
    LodgingSearchTool,
    SequenceLodgingSearchAdapter,
)
from assistant_agent.tools.plugins.builtin.lodging.plugin import (
    LodgingToolPlugin,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.registry import ToolRegistry


class _Clock:
    def __init__(self, now) -> None:
        self.now = now

    def __call__(self):
        return self.now


class _UnexpectedDefaultRuntime:
    def run_task_quantum(self, request, *, binding, cancel_token):
        raise AssertionError("hotel workflow must use its explicit profile")


def _routed_runtime(service, registry, clock):
    return DurableTaskRuntimeRouter(
        default_runtime=_UnexpectedDefaultRuntime(),
        profile_runtimes={
            "hotel_price_watch_v1": HotelPriceWatchRuntime(
                task_service=service,
                registry=registry,
                now_fn=clock,
            )
        },
    )


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="hotel-watch-user",
        session_id="hotel-watch-session",
    )


def _registry(prices: list[float | None]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        LodgingSearchTool(
            SequenceLodgingSearchAdapter(prices)
        )
    )
    return registry


def _service(task_path, outbox, registry) -> DurableTaskService:
    return DurableTaskService(
        store=SQLiteTaskStore(task_path),
        registry=registry,
        notification_outbox=outbox,
        max_model_calls=20,
        max_task_seconds=86_400,
    )


def _goal(now, *, ends_after_s: int = 600) -> HotelPriceWatchGoal:
    return HotelPriceWatchGoal(
        search=LodgingSearchRequest(
            destination="Hangzhou",
            check_in=date(2026, 8, 1),
            check_out=date(2026, 8, 3),
            adults=2,
            currency="CNY",
        ),
        max_nightly_price=600,
        check_interval_s=60,
        ends_at=now + timedelta(seconds=ends_after_s),
    )


def test_watch_restarts_stays_silent_then_notifies_once(tmp_path) -> None:
    now = utc_now()
    clock = _Clock(now)
    task_path = tmp_path / "tasks.sqlite3"
    outbox_path = tmp_path / "notifications.sqlite3"
    first_outbox = SQLiteProactiveWakeStore(outbox_path)
    first_registry = _registry([900])
    first_service = _service(task_path, first_outbox, first_registry)
    bundle = HotelPriceWatchService(first_service).create_watch(
        identity=_identity(),
        ingress_run_id="run-hotel-watch",
        goal=_goal(now),
    )
    first_worker = DurableTaskWorker(
        service=first_service,
        runtime=_routed_runtime(first_service, first_registry, clock),
        worker_id="hotel-worker-before-restart",
    )

    assert first_worker.run_once(now=clock.now) is True
    waiting = first_service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    )
    assert waiting.task.status == "waiting_schedule"
    assert waiting.task.workflow_state["last_lowest_nightly_price"] == 900
    assert first_outbox.list_outbox() == []
    first_service.store.close()

    second_outbox = SQLiteProactiveWakeStore(outbox_path)
    second_registry = _registry([900, 500])
    second_service = _service(task_path, second_outbox, second_registry)
    second_worker = DurableTaskWorker(
        service=second_service,
        runtime=_routed_runtime(second_service, second_registry, clock),
        worker_id="hotel-worker-after-restart",
    )
    clock.now = now + timedelta(seconds=59)
    assert second_worker.run_once(now=clock.now) is False

    clock.now = now + timedelta(seconds=60)
    assert second_worker.run_once(now=clock.now) is True
    unchanged = second_service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    )
    assert unchanged.task.status == "waiting_schedule"
    assert second_outbox.list_outbox() == []

    clock.now = now + timedelta(seconds=120)
    assert second_worker.run_once(now=clock.now) is True
    completed = second_service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    )
    assert completed.task.status == "completed"
    assert completed.task.workflow_state["outcome"] == "threshold_reached"
    assert completed.task.workflow_state["last_lowest_nightly_price"] == 500
    assert second_worker.run_once(now=clock.now) is False
    assert len(second_outbox.list_outbox()) == 1

    transport = MockProactiveNotificationTransport()
    delivery_worker = NotificationDeliveryWorker(
        store=second_outbox,
        transport=transport,
        delivery_observer=second_service,
        now_fn=clock,
    )
    asyncio.run(delivery_worker.drain_once())
    asyncio.run(delivery_worker.drain_once())
    assert len(transport.sent) == 1
    assert transport.sent[0].message.startswith("Sequence Hotel 当前每晚 500.00")
    second_service.store.close()


def test_provider_timeout_waits_explainably_and_cancel_stops_watch(tmp_path) -> None:
    now = utc_now()
    clock = _Clock(now)
    outbox = SQLiteProactiveWakeStore(tmp_path / "notifications.sqlite3")
    registry = _registry([None])
    service = _service(tmp_path / "tasks.sqlite3", outbox, registry)
    bundle = HotelPriceWatchService(service).create_watch(
        identity=_identity(),
        ingress_run_id="run-hotel-timeout",
        goal=_goal(now),
    )
    worker = DurableTaskWorker(
        service=service,
        runtime=_routed_runtime(service, registry, clock),
        worker_id="hotel-timeout-worker",
    )

    assert worker.run_once(now=clock.now) is True
    waiting = service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    )
    assert waiting.task.status == "waiting_schedule"
    assert waiting.task.wait is not None
    assert waiting.task.wait.reason_code == "lodging_provider_retry"
    assert waiting.task.workflow_state["last_status"] == "provider_failed"
    assert "timed out" in waiting.task.workflow_state["last_error"]

    service.cancel(
        identity=_identity(),
        task_id=bundle.task.task_id,
        reason="user_cancelled_watch",
    )
    clock.now = now + timedelta(seconds=60)
    assert worker.run_once(now=clock.now) is False
    assert outbox.list_outbox() == []
    service.store.close()


def test_watch_ends_without_booking_or_notification(tmp_path) -> None:
    now = utc_now()
    clock = _Clock(now)
    outbox = SQLiteProactiveWakeStore(tmp_path / "notifications.sqlite3")
    registry = _registry([900, 900])
    service = _service(tmp_path / "tasks.sqlite3", outbox, registry)
    bundle = HotelPriceWatchService(service).create_watch(
        identity=_identity(),
        ingress_run_id="run-hotel-expiry",
        goal=_goal(now, ends_after_s=60),
    )
    worker = DurableTaskWorker(
        service=service,
        runtime=_routed_runtime(service, registry, clock),
        worker_id="hotel-expiry-worker",
    )

    assert worker.run_once(now=clock.now) is True
    clock.now = now + timedelta(seconds=60)
    assert worker.run_once(now=clock.now) is True
    completed = service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    )
    assert completed.task.status == "completed"
    assert completed.task.workflow_state["outcome"] == "expired"
    assert outbox.list_outbox() == []
    assert service.registry.list() == ["lodging_search"]
    service.store.close()


def test_watch_creation_requires_explicit_durable_mode() -> None:
    registry = _registry([700])
    service = DurableTaskService(
        store=InMemoryTaskStore(),
        registry=registry,
        max_task_seconds=3_600,
    )
    plugin_tools = LodgingToolPlugin().build_tools(
        ToolPluginContext(
            config=ProviderConfig(
                provider_mode="mock",
                durable_tasks_enabled=True,
            ),
            mcp_server_configs=[],
            durable_task_service=service,
        )
    )
    create_tool = next(
        tool for tool in plugin_tools if tool.name == "hotel_price_watch_create"
    )
    registry.register(create_tool)
    goal = _goal(utc_now()).model_dump(mode="json")
    decision = AssistantDecision(
        type="tool_call",
        tool_name="hotel_price_watch_create",
        tool_input=goal,
        reason="Create an explicitly requested durable watch.",
    )

    foreground = UserRequest(
        user_id="hotel-watch-user",
        session_id="hotel-watch-session",
        text="foreground",
        task_execution_mode="foreground",
    )
    rejected = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=foreground,
        state=AgentState.from_request(foreground),
    )
    assert rejected.accepted is False
    assert rejected.code == "durable_plan_forbidden"

    durable = foreground.model_copy(update={"task_execution_mode": "durable"})
    state = AgentState.from_request(durable)
    accepted = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=durable,
        state=state,
    )
    assert accepted.accepted is True
    result = ToolExecutor(
        registry=registry,
        context_metadata={"durable_task_service": service},
    ).run_tool(
        state,
        "create_watch",
        "hotel_price_watch_create",
        goal,
        validated_input=accepted.validated_input,
    )
    assert result.success is True
    assert result.data is not None
    task_id = result.data["task"]["task_id"]
    created = service.get_task(identity=_identity(), task_id=task_id)
    assert created.task.execution_profile == "hotel_price_watch_v1"
    assert created.task.status == "queued"
