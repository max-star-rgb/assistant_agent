"""Identity-scoped durable task state machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

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
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.services.durable_tasks.store import TaskStore
from assistant_agent.tools.registry import ToolRegistry


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
        lease_seconds: int = 30,
    ) -> None:
        self.store = store
        self.registry = registry
        self.plan_validator = PlanValidator(max_steps=max_plan_steps)
        self.max_plan_revisions = max_plan_revisions
        self.lease_seconds = lease_seconds

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
            session_id=identity.session_id or "session_unknown",
            ingress_run_id=ingress_run_id,
            objective=plan.goal,
            status="waiting_input" if plan.requires_followup else "queued",
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
        inherited = [
            step.step_id
            for step in plan.steps
            if step.step_id in completed
            and completed[step.step_id].tool_name == step.tool_name
        ]
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
        for run in self._current_step_runs(bundle):
            if run.status not in {"succeeded", "skipped"}:
                run.status = "cancelled"
        return self.store.save(
            bundle,
            expected_version=bundle.task.version,
            events=[self._event(bundle, "task.cancelled", "cancelled", {"reason": reason})],
        )

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
        bundle.task.status = "queued"
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
        return self.store.claim_next(
            worker_id=worker_id,
            now=now or datetime.now(timezone.utc),
            lease_seconds=self.lease_seconds,
        )

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
        elif transition.kind == "completed":
            if any(
                run.status not in {"succeeded", "skipped"}
                for run in self._current_step_runs(bundle)
                if not self._plan_step(bundle, run.step_id).optional
            ):
                raise TaskTransitionRejected("required steps remain incomplete")
            bundle.task.status = "completed"
            bundle.task.terminal_at = utc_now()
            event_type = "task.completed"
        elif transition.kind in {"failed", "cancelled", "outcome_unknown"}:
            bundle.task.status = transition.kind
            if transition.kind in {"failed", "cancelled"}:
                bundle.task.terminal_at = utc_now()
            event_type = f"task.{transition.kind}"
        else:
            bundle.task.status = transition.kind
        return self.store.save(
            bundle,
            expected_version=lease.task_version,
            events=[
                self._event(
                    bundle,
                    event_type,
                    bundle.task.status,
                    {"step_id": transition.step_id, "summary": transition.summary},
                )
            ],
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
        if bundle.task.user_id != identity.user_id:
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

    @staticmethod
    def _new_step_runs(task_id: str, plan_version: TaskPlanVersion) -> list[TaskStepRun]:
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
