"""Identity-scoped durable task state machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from threading import Event, RLock
from typing import TYPE_CHECKING

from assistant_agent.agent.plan_validator import PlanValidator
from assistant_agent.schemas.durable_tasks import (
    TERMINAL_TASK_STATUSES,
    DurableTaskBundle,
    DurableTaskLease,
    DurableTaskSnapshot,
    TaskArtifactRef,
    TaskCheckpoint,
    TaskConfirmation,
    TaskEvent,
    TaskPlanVersion,
    TaskRecord,
    TaskStepRun,
    TrustedTaskBinding,
    utc_now,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.notifications import (
    NotificationEnvelope,
    NotificationOwner,
)
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.services.durable_tasks.store import TaskStore
from assistant_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from assistant_agent.services.notifications import NotificationOutbox
    from assistant_agent.services.durable_tasks.event_stream import (
        TaskEventSubscription,
    )


class DurableTaskError(RuntimeError):
    code = "durable_task_error"


class TaskNotFound(DurableTaskError):
    code = "task_not_found"


class TaskAccessDenied(DurableTaskError):
    code = "task_access_denied"


class TaskConflict(DurableTaskError):
    code = "task_conflict"


class TaskTransitionRejected(DurableTaskError):
    code = "task_transition_rejected"


class DurableTaskService:
    """Own durable task validation, transitions, leases, and snapshots."""

    def __init__(
        self,
        *,
        store: TaskStore,
        registry: ToolRegistry,
        max_plan_steps: int = 8,
        max_plan_revisions: int = 2,
        max_tool_calls: int = 32,
        max_model_calls: int = 40,
        max_step_attempts: int = 3,
        max_task_seconds: int = 3600,
        lease_seconds: int = 30,
        notification_outbox: "NotificationOutbox | None" = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.plan_validator = PlanValidator(max_steps=max_plan_steps)
        self.max_plan_revisions = max_plan_revisions
        self.max_tool_calls = max_tool_calls
        self.max_model_calls = max_model_calls
        self.max_step_attempts = max_step_attempts
        self.max_task_seconds = max_task_seconds
        self.lease_seconds = lease_seconds
        self.notification_outbox = notification_outbox
        self._cancel_events: dict[str, Event] = {}
        self._cancel_events_lock = RLock()

    def submit_plan(
        self,
        *,
        identity: RequestIdentity,
        ingress_run_id: str,
        plan: TaskPlan,
        revision_reason: str,
    ) -> DurableTaskBundle:
        self._validate_plan(plan)
        task_id = f"task_{secrets.token_hex(16)}"
        now = utc_now()
        record = TaskRecord(
            task_id=task_id,
            user_id=identity.user_id,
            agent_id=identity.agent_id,
            session_id=identity.session_id or "session_unknown",
            ingress_run_id=ingress_run_id,
            objective=plan.goal,
            status="waiting_input" if plan.requires_followup else "queued",
            remaining_budget={
                "tool_calls": self.max_tool_calls,
                "model_calls": self.max_model_calls,
                "plan_revisions": self.max_plan_revisions,
                "deadline_epoch_s": now.timestamp() + self.max_task_seconds,
            },
            created_at=now,
            updated_at=now,
        )
        version = TaskPlanVersion(
            task_id=task_id,
            plan_version=1,
            plan=plan,
            revision_reason=revision_reason,
            created_at=now,
        )
        bundle = DurableTaskBundle(
            task=record,
            plans=[version],
            step_runs=self._new_step_runs(task_id, version),
        )
        return self.store.create(
            bundle,
            [
                self._event(bundle, "task.accepted", record.status),
                self._event(
                    bundle,
                    "plan.created",
                    record.status,
                    {"plan_version": 1, "step_count": len(plan.steps)},
                ),
            ],
        )

    def revise_plan(
        self,
        *,
        binding: TrustedTaskBinding,
        plan: TaskPlan,
        revision_reason: str,
    ) -> DurableTaskBundle:
        self._validate_plan(plan)
        bundle = self._bound_bundle(binding)
        if len(bundle.plans) - 1 >= self.max_plan_revisions:
            raise TaskConflict("plan revision limit reached")
        previous = self._current_plan(bundle)
        completed = {
            run.step_id: run
            for run in bundle.step_runs
            if run.plan_version == previous.plan_version and run.status == "succeeded"
        }
        previous_steps = {step.step_id: step for step in previous.plan.steps}
        inherited = [
            step.step_id
            for step in plan.steps
            if step.step_id in completed
            and completed[step.step_id].tool_name == step.tool_name
            and previous.plan.goal == plan.goal
            and previous_steps.get(step.step_id) == step
        ]
        invalidated_confirmations = [
            item
            for item in bundle.confirmations
            if item.plan_version == previous.plan_version
            and item.status in {"pending", "approved"}
        ]
        for confirmation in invalidated_confirmations:
            confirmation.status = "rejected"
            confirmation.decided_at = utc_now()
        next_version = bundle.task.current_plan_version + 1
        plan_version = TaskPlanVersion(
            task_id=bundle.task.task_id,
            plan_version=next_version,
            plan=plan,
            revision_reason=revision_reason,
            inherited_step_ids=inherited,
            replaced_step_ids=[
                step.step_id for step in previous.plan.steps if step.step_id not in inherited
            ],
            invalidated_confirmation_ids=[
                item.confirmation_id for item in invalidated_confirmations
            ],
        )
        bundle.plans.append(plan_version)
        new_runs = self._new_step_runs(bundle.task.task_id, plan_version)
        for run in new_runs:
            if run.step_id in inherited:
                prior = completed[run.step_id]
                run.status = "succeeded"
                run.output_ref = prior.output_ref
                run.summary = prior.summary
        bundle.step_runs.extend(new_runs)
        bundle.task.current_plan_version = next_version
        bundle.task.objective = plan.goal
        bundle.task.remaining_budget["plan_revisions"] = max(
            0,
            int(bundle.task.remaining_budget.get("plan_revisions", 0)) - 1,
        )
        bundle.task.status = "waiting_input" if plan.requires_followup else "running"
        self._refresh_ready_steps(bundle)
        return self.store.save(
            bundle,
            expected_version=binding.task_version,
            events=[
                self._event(
                    bundle,
                    "plan.revised",
                    bundle.task.status,
                    {"plan_version": next_version, "inherited_step_ids": inherited},
                )
            ],
        )

    def get_task(self, *, identity: RequestIdentity, task_id: str) -> DurableTaskBundle:
        bundle = self.store.load(task_id)
        if bundle is None:
            raise TaskNotFound(task_id)
        self._require_identity(identity, bundle)
        return bundle

    def list_events(
        self,
        *,
        identity: RequestIdentity,
        task_id: str,
        after: int,
        limit: int,
    ) -> list[TaskEvent]:
        self.get_task(identity=identity, task_id=task_id)
        return self.store.list_events(task_id, after=after, limit=min(max(limit, 1), 500))

    def subscribe_events(
        self,
        *,
        identity: RequestIdentity,
        task_id: str,
        after: int = 0,
        batch_size: int = 100,
        poll_seconds: float = 0.25,
        stop_on_quiescent: bool = True,
    ) -> "TaskEventSubscription":
        """Return an identity-scoped replay/tail view over persisted TaskEvents."""

        from assistant_agent.services.durable_tasks.event_stream import (
            TaskEventSubscription,
        )

        self.get_task(identity=identity, task_id=task_id)
        return TaskEventSubscription(
            service=self,
            identity=identity,
            task_id=task_id,
            after=after,
            batch_size=batch_size,
            poll_seconds=poll_seconds,
            stop_on_quiescent=stop_on_quiescent,
        )

    def cancel(
        self,
        *,
        identity: RequestIdentity,
        task_id: str,
        reason: str,
    ) -> DurableTaskBundle:
        bundle = self.get_task(identity=identity, task_id=task_id)
        if bundle.task.status == "cancelled":
            return bundle
        if bundle.task.status in {"completed", "failed"}:
            raise TaskConflict("terminal task cannot be cancelled")
        bundle.task.status = "cancelled"
        bundle.task.terminal_at = utc_now()
        bundle.task.lease_owner = None
        bundle.task.lease_token = None
        bundle.task.lease_expires_at = None
        bundle.task.wait = None
        for run in self._current_step_runs(bundle):
            if run.status not in {"succeeded", "skipped"}:
                run.status = "cancelled"
        saved = self.store.save(
            bundle,
            expected_version=bundle.task.version,
            events=[self._event(bundle, "task.cancelled", "cancelled", {"reason": reason})],
        )
        self.task_cancel_token(task_id).set()
        return saved

    def task_cancel_token(self, task_id: str) -> Event:
        """Return the process-local cooperative token for an active task."""

        with self._cancel_events_lock:
            return self._cancel_events.setdefault(task_id, Event())

    def provide_input(
        self,
        *,
        identity: RequestIdentity,
        task_id: str,
        text: str,
    ) -> DurableTaskBundle:
        bundle = self.get_task(identity=identity, task_id=task_id)
        if bundle.task.status != "waiting_input":
            raise TaskTransitionRejected("task is not waiting for input")
        bundle.task.active_constraints.append(f"User-provided task input: {text}")
        bundle.task.status = "queued"
        for run in self._current_step_runs(bundle):
            if run.status == "waiting_input":
                run.status = "pending"
        self._refresh_ready_steps(bundle)
        return self.store.save(
            bundle,
            expected_version=bundle.task.version,
            events=[self._event(bundle, "task.input_received", "queued", {"text_chars": len(text)})],
        )

    def confirm(
        self,
        *,
        identity: RequestIdentity,
        task_id: str,
        confirmation_id: str,
        approved: bool,
    ) -> DurableTaskBundle:
        bundle = self.get_task(identity=identity, task_id=task_id)
        confirmation = next(
            (
                item
                for item in bundle.confirmations
                if item.confirmation_id == confirmation_id
            ),
            None,
        )
        if confirmation is None:
            raise TaskNotFound(confirmation_id)
        if confirmation.status != "pending":
            return bundle
        if confirmation.expires_at <= utc_now():
            confirmation.status = "expired"
            confirmation.decided_at = utc_now()
            run = self._step_run(bundle, confirmation.step_id)
            if run is not None:
                run.status = "failed"
                run.error_code = "confirmation_expired"
            bundle.task.status = "replanning"
            self.store.save(
                bundle,
                expected_version=bundle.task.version,
                events=[
                    self._event(
                        bundle,
                        "confirmation.expired",
                        bundle.task.status,
                        {"confirmation_id": confirmation.confirmation_id},
                    )
                ],
            )
            raise TaskConflict("confirmation expired")
        expected_digest = _confirmation_digest(
            task_id=confirmation.task_id,
            plan_version=confirmation.plan_version,
            step_id=confirmation.step_id,
            tool_name=confirmation.tool_name,
            input_digest=confirmation.input_digest,
            expires_at=confirmation.expires_at,
        )
        if not hmac.compare_digest(confirmation.binding_digest, expected_digest):
            raise TaskConflict("confirmation binding is invalid")
        confirmation.status = "approved" if approved else "rejected"
        confirmation.decided_by_user_id = identity.user_id
        confirmation.decided_at = utc_now()
        run = self._step_run(bundle, confirmation.step_id)
        if run is not None:
            run.status = "ready" if approved else "failed"
        bundle.task.status = "queued" if approved else "replanning"
        return self.store.save(
            bundle,
            expected_version=bundle.task.version,
            events=[
                self._event(
                    bundle,
                    "confirmation.approved" if approved else "confirmation.rejected",
                    bundle.task.status,
                    {
                        "confirmation_id": confirmation.confirmation_id,
                        "step_id": confirmation.step_id,
                    },
                )
            ],
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> DurableTaskLease | None:
        claimed_at = now or datetime.now(timezone.utc)
        while True:
            lease = self.store.claim_next(
                worker_id=worker_id,
                now=claimed_at,
                lease_seconds=self.lease_seconds,
            )
            if lease is None:
                return None
            bundle = self._lease_bundle(lease)
            if bundle.task.wait is not None:
                wait = bundle.task.wait
                if wait.expires_at is not None and wait.expires_at <= claimed_at:
                    self.checkpoint(
                        lease,
                        TaskCheckpoint(
                            kind="failed",
                            step_id=wait.step_id,
                            summary="Scheduled task wait expired before resumption.",
                            error_code="durable_wait_expired",
                            error_message="The scheduled wait expired.",
                        ),
                    )
                    continue
                lease, bundle = self._resume_scheduled_wait(
                    lease=lease,
                    bundle=bundle,
                    resumed_at=claimed_at,
                )
            interrupted = next(
                (run for run in self._current_step_runs(bundle) if run.status == "running"),
                None,
            )
            if interrupted is None:
                admitted = self._admit_quantum(lease, bundle, claimed_at)
                if admitted is not None:
                    return admitted
                continue
            if interrupted.side_effect_level in {"none", "local_read", "external_read"}:
                if interrupted.attempt >= self.max_step_attempts:
                    self.checkpoint(
                        lease,
                        TaskCheckpoint(
                            kind="failed",
                            step_id=interrupted.step_id,
                            error_code="durable_step_attempts_exhausted",
                            error_message="Read-only step retry budget exhausted after crash recovery.",
                        ),
                    )
                    continue
                interrupted.status = "ready"
                recovered = self.store.save(
                    bundle,
                    expected_version=lease.task_version,
                    events=[
                        self._event(
                            bundle,
                            "step.retry_scheduled",
                            bundle.task.status,
                            {"step_id": interrupted.step_id, "attempt": interrupted.attempt},
                        )
                    ],
                )
                recovered_lease = lease.model_copy(
                    update={"task_version": recovered.task.version}
                )
                admitted = self._admit_quantum(recovered_lease, recovered, claimed_at)
                if admitted is not None:
                    return admitted
                continue
            self.checkpoint(
                lease,
                TaskCheckpoint(
                    kind="outcome_unknown",
                    step_id=interrupted.step_id,
                    summary="Worker lease expired after a possible external side effect.",
                    error_code="mutating_outcome_unknown",
                    error_message="External commit state requires reconciliation.",
                ),
            )

    def _admit_quantum(
        self,
        lease: DurableTaskLease,
        bundle: DurableTaskBundle,
        now: datetime,
    ) -> DurableTaskLease | None:
        remaining = int(bundle.task.remaining_budget.get("model_calls", 0))
        deadline = float(bundle.task.remaining_budget.get("deadline_epoch_s", 0))
        if remaining <= 0 or (deadline > 0 and now.timestamp() >= deadline):
            self.checkpoint(
                lease,
                TaskCheckpoint(
                    kind="failed",
                    error_code="durable_task_budget_exhausted",
                    error_message="Durable task model-call or time budget exhausted.",
                ),
            )
            return None
        bundle.task.remaining_budget["model_calls"] = remaining - 1
        saved = self.store.save(
            bundle,
            expected_version=lease.task_version,
            events=[
                self._event(
                    bundle,
                    "task.quantum_admitted",
                    bundle.task.status,
                    {"remaining_model_calls": remaining - 1},
                )
            ],
        )
        return lease.model_copy(update={"task_version": saved.task.version})

    def begin_attempt(
        self,
        *,
        binding: TrustedTaskBinding,
        step_id: str,
        tool_name: str,
        tool_input_digest: str,
    ) -> TrustedTaskBinding:
        """Persist the external-call boundary before ToolExecutor can run."""

        bundle = self._bound_bundle(binding)
        if bundle.task.status != "running":
            raise TaskConflict("task is no longer executable")
        run = self._step_run(bundle, step_id)
        if (
            run is None
            or run.status != "ready"
            or step_id not in binding.ready_step_ids
            or run.tool_name != tool_name
        ):
            raise TaskTransitionRejected("step is not ready for this tool attempt")
        remaining = int(bundle.task.remaining_budget.get("tool_calls", 0))
        if remaining <= 0 or run.attempt >= self.max_step_attempts:
            raise TaskTransitionRejected("durable tool-call budget exhausted")
        run.status = "running"
        run.attempt += 1
        run.started_at = utc_now()
        run.tool_input_digest = tool_input_digest
        bundle.task.remaining_budget["tool_calls"] = remaining - 1
        saved = self.store.save(
            bundle,
            expected_version=binding.task_version,
            events=[
                self._event(
                    bundle,
                    "step.started",
                    bundle.task.status,
                    {"step_id": step_id, "attempt": run.attempt},
                )
            ],
        )
        return binding.model_copy(update={"task_version": saved.task.version})

    def snapshot_for_lease(self, lease: DurableTaskLease) -> DurableTaskSnapshot:
        bundle = self._lease_bundle(lease)
        plan = self._current_plan(bundle)
        runs = self._current_step_runs(bundle)
        return DurableTaskSnapshot(
            task_id=bundle.task.task_id,
            objective=bundle.task.objective,
            active_constraints=list(bundle.task.active_constraints),
            task_status=bundle.task.status,
            plan_version=plan.plan_version,
            plan=plan.plan,
            ready_step_ids=[run.step_id for run in runs if run.status == "ready"],
            completed_steps=[
                {"step_id": run.step_id, "summary": run.summary, "output_ref": run.output_ref or ""}
                for run in runs
                if run.status == "succeeded"
            ],
            artifact_refs=list(bundle.artifacts),
            wait=bundle.task.wait,
            remaining_budget=dict(bundle.task.remaining_budget),
        )

    def checkpoint(
        self,
        lease: DurableTaskLease,
        transition: TaskCheckpoint,
    ) -> DurableTaskBundle:
        bundle = self._lease_bundle(lease)
        if bundle.task.status in TERMINAL_TASK_STATUSES:
            raise TaskConflict("terminal task cannot checkpoint")
        run = self._step_run(bundle, transition.step_id) if transition.step_id else None
        event_type = f"task.{transition.kind}"
        if transition.kind == "tool_succeeded":
            if run is None or run.status not in {"ready", "leased", "running"}:
                raise TaskTransitionRejected("step is not ready for success checkpoint")
            run.status = "succeeded"
            run.output_ref = transition.output_ref
            run.summary = transition.summary
            run.finished_at = utc_now()
            if transition.output_ref:
                bundle.artifacts.append(
                    TaskArtifactRef(
                        artifact_ref=transition.output_ref,
                        kind="tool_result",
                        summary=transition.summary,
                        producer_plan_version=bundle.task.current_plan_version,
                        producer_step_id=run.step_id,
                    )
                )
            self._refresh_ready_steps(bundle)
            bundle.task.status = "running"
            event_type = "step.completed"
        elif transition.kind == "tool_failed":
            if run is None:
                raise TaskTransitionRejected("tool failure requires step_id")
            run.status = "failed"
            run.error_code = transition.error_code
            run.error_message = transition.error_message
            bundle.task.status = "replanning"
            event_type = "step.failed"
        elif transition.kind == "waiting_schedule":
            wait = transition.wait
            if wait is None:
                raise TaskTransitionRejected(
                    "scheduled wait checkpoint requires wait"
                )
            if transition.step_id and wait.step_id not in {
                None,
                transition.step_id,
            }:
                raise TaskTransitionRejected(
                    "scheduled wait step does not match checkpoint step"
                )
            if wait.next_eligible_at <= utc_now():
                raise TaskTransitionRejected(
                    "scheduled wait must target a future time"
                )
            if run is not None:
                if run.status not in {"ready", "leased", "running"}:
                    raise TaskTransitionRejected(
                        "step is not ready for scheduled wait"
                    )
                run.status = "waiting_schedule"
            bundle.task.wait = wait.model_copy(
                update={"step_id": transition.step_id or wait.step_id}
            )
            bundle.task.status = "waiting_schedule"
            bundle.task.lease_owner = None
            bundle.task.lease_token = None
            bundle.task.lease_expires_at = None
            event_type = "task.wait_scheduled"
        elif transition.kind == "waiting_confirmation":
            if (
                run is None
                or not transition.tool_name
                or not transition.tool_input_digest
                or transition.confirmation_expires_at is None
            ):
                raise TaskTransitionRejected(
                    "confirmation checkpoint requires step, tool, input digest, and expiry"
                )
            confirmation = TaskConfirmation(
                confirmation_id=f"confirm_{secrets.token_hex(12)}",
                task_id=bundle.task.task_id,
                plan_version=bundle.task.current_plan_version,
                step_id=run.step_id,
                tool_name=transition.tool_name,
                input_digest=transition.tool_input_digest,
                binding_digest=_confirmation_digest(
                    task_id=bundle.task.task_id,
                    plan_version=bundle.task.current_plan_version,
                    step_id=run.step_id,
                    tool_name=transition.tool_name,
                    input_digest=transition.tool_input_digest,
                    expires_at=transition.confirmation_expires_at,
                ),
                summary=transition.confirmation_summary
                or f"Approve {transition.tool_name} for durable step {run.step_id}.",
                expires_at=transition.confirmation_expires_at,
            )
            bundle.confirmations.append(confirmation)
            run.status = "waiting_confirmation"
            run.tool_input_digest = transition.tool_input_digest
            bundle.task.status = "waiting_confirmation"
            bundle.task.lease_owner = None
            bundle.task.lease_token = None
            bundle.task.lease_expires_at = None
            event_type = "confirmation.required"
        elif transition.kind == "waiting_input":
            if run is not None:
                run.status = "waiting_input"
            bundle.task.status = "waiting_input"
            bundle.task.lease_owner = None
            bundle.task.lease_token = None
            bundle.task.lease_expires_at = None
            event_type = "task.input_required"
        elif transition.kind == "completed":
            if any(
                run.status not in {"succeeded", "skipped"}
                for run in self._current_step_runs(bundle)
                if not self._plan_step(bundle, run.step_id).optional
            ):
                raise TaskTransitionRejected("required steps remain incomplete")
            bundle.task.status = "completed"
            bundle.task.wait = None
            bundle.task.terminal_at = utc_now()
            event_type = "task.completed"
        elif transition.kind in {"failed", "cancelled", "outcome_unknown"}:
            bundle.task.status = transition.kind
            if run is not None:
                run.status = transition.kind
                run.error_code = transition.error_code
                run.error_message = transition.error_message
                run.finished_at = utc_now()
            bundle.task.lease_owner = None
            bundle.task.lease_token = None
            bundle.task.lease_expires_at = None
            bundle.task.wait = None
            if transition.kind in {"failed", "cancelled"}:
                bundle.task.terminal_at = utc_now()
            event_type = f"task.{transition.kind}"
        else:
            bundle.task.status = transition.kind
        notification = self._enqueue_task_notification(bundle, transition)
        events = [
            self._event(
                bundle,
                event_type,
                bundle.task.status,
                (
                    {
                        "step_id": transition.step_id,
                        "summary": transition.summary,
                        "reason_code": transition.wait.reason_code,
                        "next_eligible_at": (
                            transition.wait.next_eligible_at.isoformat()
                        ),
                        "expires_at": (
                            transition.wait.expires_at.isoformat()
                            if transition.wait.expires_at is not None
                            else None
                        ),
                    }
                    if transition.kind == "waiting_schedule"
                    and transition.wait is not None
                    else {
                        "step_id": transition.step_id,
                        "summary": transition.summary,
                    }
                ),
            )
        ]
        if notification is not None:
            events.append(
                self._event(
                    bundle,
                    "notification.enqueued",
                    bundle.task.status,
                    _notification_event_payload(notification),
                )
            )
        return self.store.save(
            bundle,
            expected_version=lease.task_version,
            events=events,
        )

    def record_notification_delivery(
        self,
        notification: NotificationEnvelope,
    ) -> None:
        """Project a durable-task notification transition into TaskEvent."""

        if notification.origin_kind != "durable_task":
            return
        task_id = notification.origin_ref
        if not task_id:
            raise TaskTransitionRejected("durable notification has no task origin")
        bundle = self.store.load(task_id)
        if bundle is None:
            raise TaskNotFound(task_id)
        if (
            bundle.task.user_id != notification.owner.user_id
            or bundle.task.agent_id != notification.owner.agent_id
        ):
            raise TaskAccessDenied(task_id)
        payload = _notification_event_payload(notification)
        event_type = f"notification.{notification.status}"
        existing = self.store.list_events(task_id, after=0, limit=500)
        if any(
            event.event_type == event_type
            and event.payload.get("delivery_id") == notification.delivery_id
            and event.payload.get("attempt_count") == notification.attempt_count
            for event in existing
        ):
            return
        self.store.save(
            bundle,
            expected_version=bundle.task.version,
            events=[
                self._event(
                    bundle,
                    event_type,
                    bundle.task.status,
                    payload,
                )
            ],
        )

    def _enqueue_task_notification(
        self,
        bundle: DurableTaskBundle,
        transition: TaskCheckpoint,
    ) -> NotificationEnvelope | None:
        request = transition.notification
        if request is None:
            return None
        if self.notification_outbox is None:
            raise TaskTransitionRejected(
                "durable notification outbox is not configured"
            )
        task_id = bundle.task.task_id
        namespaced_key = hashlib.sha256(
            f"durable_task|{task_id}|{request.idempotency_key}".encode()
        ).hexdigest()
        notification = NotificationEnvelope(
            owner=NotificationOwner(
                user_id=bundle.task.user_id,
                agent_id=bundle.task.agent_id,
            ),
            channel=request.channel,
            destination_ref=f"user:{bundle.task.user_id}",
            message=request.message,
            idempotency_key=namespaced_key,
            rule_id=f"durable_task:{task_id}",
            origin_kind="durable_task",
            origin_ref=task_id,
            evidence_ids=request.evidence_ids,
            evidence_fingerprint=request.evidence_fingerprint,
            deliver_after=request.deliver_after,
            expires_at=request.expires_at,
        )
        return self.notification_outbox.enqueue_notification(notification)

    def _resume_scheduled_wait(
        self,
        *,
        lease: DurableTaskLease,
        bundle: DurableTaskBundle,
        resumed_at: datetime,
    ) -> tuple[DurableTaskLease, DurableTaskBundle]:
        wait = bundle.task.wait
        if wait is None or wait.kind != "schedule":
            raise TaskConflict("claimed task does not have a scheduled wait")
        if wait.next_eligible_at > resumed_at:
            raise TaskConflict("scheduled task was claimed before it became due")
        if wait.step_id is not None:
            run = self._step_run(bundle, wait.step_id)
            if run is None or run.status != "waiting_schedule":
                raise TaskConflict("scheduled wait step is not resumable")
            run.status = "ready"
        bundle.task.wait = None
        saved = self.store.save(
            bundle,
            expected_version=lease.task_version,
            events=[
                self._event(
                    bundle,
                    "task.wake_received",
                    bundle.task.status,
                    {
                        "reason_code": wait.reason_code,
                        "scheduled_for": wait.next_eligible_at.isoformat(),
                    },
                ),
                self._event(
                    bundle,
                    "task.resumed",
                    bundle.task.status,
                    {"step_id": wait.step_id, "resume_source": "schedule"},
                ),
            ],
        )
        return (
            lease.model_copy(update={"task_version": saved.task.version}),
            saved,
        )

    def _validate_plan(self, plan: TaskPlan) -> None:
        result = self.plan_validator.validate(plan, self.registry)
        if not result.accepted:
            raise TaskTransitionRejected(result.message)

    def _bound_bundle(self, binding: TrustedTaskBinding) -> DurableTaskBundle:
        bundle = self.store.load(binding.task_id)
        if bundle is None:
            raise TaskNotFound(binding.task_id)
        if (
            bundle.task.version != binding.task_version
            or bundle.task.current_plan_version != binding.plan_version
            or bundle.task.lease_owner != binding.lease_owner
            or bundle.task.lease_token != binding.lease_token
        ):
            raise TaskConflict("trusted task binding is stale")
        return bundle

    def _lease_bundle(self, lease: DurableTaskLease) -> DurableTaskBundle:
        bundle = self.store.load(lease.task_id)
        if bundle is None:
            raise TaskNotFound(lease.task_id)
        if (
            bundle.task.version != lease.task_version
            or bundle.task.lease_owner != lease.worker_id
            or bundle.task.lease_token != lease.lease_token
        ):
            raise TaskConflict("task lease is stale")
        return bundle

    @staticmethod
    def _require_identity(identity: RequestIdentity, bundle: DurableTaskBundle) -> None:
        if (
            bundle.task.user_id != identity.user_id
            or bundle.task.agent_id != identity.agent_id
        ):
            raise TaskAccessDenied(bundle.task.task_id)

    @staticmethod
    def _current_plan(bundle: DurableTaskBundle) -> TaskPlanVersion:
        return next(
            plan for plan in bundle.plans if plan.plan_version == bundle.task.current_plan_version
        )

    @staticmethod
    def _current_step_runs(bundle: DurableTaskBundle) -> list[TaskStepRun]:
        return [
            run for run in bundle.step_runs if run.plan_version == bundle.task.current_plan_version
        ]

    def _step_run(self, bundle: DurableTaskBundle, step_id: str | None) -> TaskStepRun | None:
        return next(
            (run for run in self._current_step_runs(bundle) if run.step_id == step_id),
            None,
        )

    def _plan_step(self, bundle: DurableTaskBundle, step_id: str) -> TaskStep:
        return next(step for step in self._current_plan(bundle).plan.steps if step.step_id == step_id)

    def _new_step_runs(self, task_id: str, plan_version: TaskPlanVersion) -> list[TaskStepRun]:
        return [
            TaskStepRun(
                task_id=task_id,
                plan_version=plan_version.plan_version,
                step_id=step.step_id,
                status="ready" if not step.depends_on and not plan_version.plan.requires_followup else "pending",
                idempotency_key=_idempotency_key(
                    task_id,
                    plan_version.plan_version,
                    step.step_id,
                    step.tool_name,
                ),
                tool_name=step.tool_name,
                side_effect_level=_durable_side_effect_level(
                    self.registry,
                    step.tool_name,
                ),
            )
            for step in plan_version.plan.steps
        ]

    def _refresh_ready_steps(self, bundle: DurableTaskBundle) -> None:
        runs = {run.step_id: run for run in self._current_step_runs(bundle)}
        for step in self._current_plan(bundle).plan.steps:
            run = runs[step.step_id]
            if run.status not in {"pending", "ready"}:
                continue
            run.status = (
                "ready"
                if all(runs[dependency].status == "succeeded" for dependency in step.depends_on)
                else "pending"
            )

    @staticmethod
    def _event(
        bundle: DurableTaskBundle,
        event_type: str,
        status: str,
        payload: dict | None = None,
    ) -> TaskEvent:
        return TaskEvent(
            task_id=bundle.task.task_id,
            event_type=event_type,
            status=status,
            payload=payload or {},
        )


def _idempotency_key(
    task_id: str,
    plan_version: int,
    step_id: str,
    tool_name: str | None,
) -> str:
    raw = f"{task_id}:{plan_version}:{step_id}:{tool_name or ''}"
    return "task:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _durable_side_effect_level(
    registry: ToolRegistry,
    tool_name: str | None,
) -> str:
    if tool_name is None:
        return "none"
    return (
        "external_read"
        if registry.get_spec(tool_name).category == "read"
        else "possible_write"
    )


def _notification_event_payload(
    notification: NotificationEnvelope,
) -> dict[str, object]:
    return {
        "delivery_id": notification.delivery_id,
        "channel": notification.channel,
        "status": notification.status,
        "attempt_count": notification.attempt_count,
        "reason_code": notification.last_reason_code,
    }


def _confirmation_digest(
    *,
    task_id: str,
    plan_version: int,
    step_id: str,
    tool_name: str,
    input_digest: str,
    expires_at: datetime,
) -> str:
    payload = {
        "task_id": task_id,
        "plan_version": plan_version,
        "step_id": step_id,
        "tool_name": tool_name,
        "input_digest": input_digest,
        "expires_at": expires_at.isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
