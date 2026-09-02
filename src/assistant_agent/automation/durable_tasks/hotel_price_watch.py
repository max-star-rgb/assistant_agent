"""Deterministic hotel-price monitoring workflow over governed read tools."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta

from assistant_agent.runtime.state import AgentState
from assistant_agent.automation.durable_tasks.models import (
    DurableTaskSnapshot,
    TaskCheckpoint,
    TaskNotificationRequest,
    TaskPlan,
    TaskStep,
    TaskWaitState,
    TrustedTaskBinding,
    utc_now,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.tools.plugins.builtin.lodging.models import (
    HotelPriceWatchGoal,
    LodgingOffer,
    LodgingSearchRequest,
)
from assistant_agent.tools.plugins.builtin.lodging.backend import (
    LodgingSearchAdapter,
    MockLodgingSearchAdapter,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.worker import TaskQuantumResult

HOTEL_PRICE_WATCH_PROFILE = "hotel_price_watch_v1"
LODGING_SEARCH_TOOL_NAME = "lodging_search"


class HotelPriceWatchService:
    """Create structured, read-only hotel watches."""

    def __init__(self, task_service: DurableTaskService) -> None:
        self.task_service = task_service

    def create_watch(
        self,
        *,
        identity: RequestIdentity,
        ingress_run_id: str,
        goal: HotelPriceWatchGoal,
    ):
        remaining_seconds = max(0.0, (goal.ends_at - utc_now()).total_seconds())
        required_quanta = math.ceil(
            remaining_seconds / goal.check_interval_s
        ) + 1
        if required_quanta > self.task_service.max_workflow_quanta:
            raise ValueError(
                "hotel watch interval exceeds the configured durable quantum budget"
            )
        return self.task_service.submit_plan(
            identity=identity,
            ingress_run_id=ingress_run_id,
            plan=TaskPlan(
                goal=(
                    f"Monitor lodging prices for {goal.search.destination} until "
                    f"{goal.ends_at.isoformat()} without booking or payment."
                ),
                steps=[
                    TaskStep(
                        step_id="lodging_probe",
                        action="check current lodging offers",
                        tool_name=LODGING_SEARCH_TOOL_NAME,
                        optional=True,
                        reason="Read-only recurring price observation.",
                    )
                ],
            ),
            revision_reason="hotel_price_watch_created",
            execution_profile=HOTEL_PRICE_WATCH_PROFILE,
            workflow_payload=goal.model_dump(mode="json"),
            deadline_at=goal.ends_at + timedelta(seconds=1),
        )


class HotelPriceWatchRuntime:
    """Execute one bounded search/checkpoint quantum without an LLM call."""

    def __init__(
        self,
        *,
        task_service: DurableTaskService,
        adapter: LodgingSearchAdapter | None = None,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self.task_service = task_service
        self.adapter = adapter or MockLodgingSearchAdapter()
        self.now_fn = now_fn

    def run_task_quantum(
        self,
        request: UserRequest,
        *,
        binding: TrustedTaskBinding,
        cancel_token,
    ) -> TaskQuantumResult:
        snapshot = DurableTaskSnapshot.model_validate(
            request.metadata["durable_task_snapshot"]
        )
        goal = HotelPriceWatchGoal.model_validate(snapshot.workflow_payload)
        state = AgentState.from_request(request)
        now = self.now_fn()
        if now >= goal.ends_at:
            fingerprint = _digest({
                "task_id": snapshot.task_id,
                "outcome": "expired",
                "ends_at": goal.ends_at.isoformat(),
            })
            return TaskQuantumResult(
                checkpoint=TaskCheckpoint(
                    kind="completed",
                    summary="Hotel price watch ended without a matching offer.",
                    notification=TaskNotificationRequest(
                        channel=goal.notification_channel,
                        message=(
                            f"{goal.search.destination}酒店价格监控已结束，"
                            "期间未找到符合预算的报价。"
                        ),
                        idempotency_key=f"expired:{fingerprint}",
                        evidence_ids=[f"task:{snapshot.task_id}"],
                        evidence_fingerprint=fingerprint,
                        deliver_after=now,
                        expires_at=now + timedelta(hours=6),
                    ),
                    workflow_state_patch={
                        "outcome": "expired",
                        "ended_at": now.isoformat(),
                    },
                ),
                state=state,
                binding=binding,
            )

        step_id = _ready_probe_step(snapshot, binding)
        if goal.starts_at is not None and now < goal.starts_at:
            return TaskQuantumResult(
                checkpoint=TaskCheckpoint(
                    kind="waiting_schedule",
                    step_id=step_id,
                    wait=TaskWaitState(
                        kind="schedule",
                        reason_code="hotel_watch_not_started",
                        summary="Waiting for the configured first hotel price check.",
                        step_id=step_id,
                        next_eligible_at=goal.starts_at,
                        expires_at=goal.ends_at + timedelta(seconds=1),
                    ),
                    workflow_state_patch={
                        "scheduled_first_check_at": goal.starts_at.isoformat(),
                    },
                ),
                state=state,
                binding=binding,
            )
        tool_input = goal.search.model_dump(mode="json")
        active_binding = self.task_service.begin_attempt(
            binding=binding,
            step_id=step_id,
            tool_name=LODGING_SEARCH_TOOL_NAME,
            tool_input_digest=_digest(tool_input),
        )
        request.metadata["durable_task_binding"] = active_binding.model_dump(
            mode="json"
        )
        result = self.adapter.search(
            LodgingSearchRequest.model_validate(goal.search.model_dump())
        )
        next_check_at = min(
            now + timedelta(seconds=goal.check_interval_s),
            goal.ends_at,
        )
        wait = TaskWaitState(
            kind="schedule",
            reason_code=(
                "lodging_provider_retry"
                if not result.success
                else "hotel_price_above_threshold"
            ),
            summary=(
                "Lodging provider failed; waiting for the next bounded retry."
                if not result.success
                else "No offer is at or below the configured nightly budget."
            ),
            step_id=step_id,
            next_eligible_at=next_check_at,
            expires_at=goal.ends_at + timedelta(seconds=1),
        )
        if not result.success:
            return TaskQuantumResult(
                checkpoint=TaskCheckpoint(
                    kind="waiting_schedule",
                    step_id=step_id,
                    wait=wait,
                    workflow_state_patch={
                        "last_status": "provider_failed",
                        "last_error": (
                            result.error_message or "lodging_search_failed"
                        ),
                        "last_checked_at": now.isoformat(),
                    },
                ),
                state=state,
                binding=active_binding,
            )

        best = _best_offer(result.offers)
        fingerprint = _offer_fingerprint(result.offers)
        state_patch = {
            "last_status": "observed",
            "last_checked_at": now.isoformat(),
            "last_evidence_fingerprint": fingerprint,
            "last_lowest_nightly_price": (
                best.nightly_price if best is not None else None
            ),
        }
        if best is None or best.nightly_price > goal.max_nightly_price:
            return TaskQuantumResult(
                checkpoint=TaskCheckpoint(
                    kind="waiting_schedule",
                    step_id=step_id,
                    wait=wait,
                    workflow_state_patch=state_patch,
                ),
                state=state,
                binding=active_binding,
            )

        message = (
            f"{best.property_name} 当前每晚 {best.nightly_price:.2f} "
            f"{best.currency}，已达到你设置的 {goal.max_nightly_price:.2f} "
            f"{goal.search.currency} 预算。"
        )
        notification = TaskNotificationRequest(
            channel=goal.notification_channel,
            message=message,
            idempotency_key=f"threshold:{fingerprint}",
            evidence_ids=[best.source_ref],
            evidence_fingerprint=fingerprint,
            deliver_after=now,
            expires_at=max(goal.ends_at, now + timedelta(hours=6)),
        )
        return TaskQuantumResult(
            checkpoint=TaskCheckpoint(
                kind="completed",
                step_id=step_id,
                summary="A hotel offer reached the configured nightly budget.",
                notification=notification,
                workflow_state_patch={
                    **state_patch,
                    "outcome": "threshold_reached",
                    "matched_offer_id": best.offer_id,
                },
            ),
            state=state,
            binding=active_binding,
        )


def _ready_probe_step(
    snapshot: DurableTaskSnapshot,
    binding: TrustedTaskBinding,
) -> str:
    ready = [
        step.step_id
        for step in snapshot.plan.steps
        if step.step_id in binding.ready_step_ids
        and step.tool_name == LODGING_SEARCH_TOOL_NAME
    ]
    if len(ready) != 1:
        raise RuntimeError("hotel price watch requires one ready lodging probe")
    return ready[0]


def _best_offer(offers: list[LodgingOffer]) -> LodgingOffer | None:
    return min(
        offers,
        key=lambda item: (item.nightly_price, item.offer_id),
        default=None,
    )


def _offer_fingerprint(offers: list[LodgingOffer]) -> str:
    payload = [
        {
            "offer_id": offer.offer_id,
            "nightly_price": offer.nightly_price,
            "currency": offer.currency,
            "refundable": offer.refundable,
        }
        for offer in sorted(offers, key=lambda item: item.offer_id)
    ]
    return _digest(payload)


def _digest(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
