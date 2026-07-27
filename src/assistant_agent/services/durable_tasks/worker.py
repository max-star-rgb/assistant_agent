"""Cooperative one-quantum durable task worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.durable_tasks import (
    DurableTaskLease,
    TaskCheckpoint,
    TrustedTaskBinding,
)
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.durable_tasks.service import DurableTaskService, TaskConflict
from assistant_agent.services.durable_tasks.store import TaskLeaseConflict

if TYPE_CHECKING:
    from assistant_agent.agent.runtime import AgentGraphRuntime


class DurableTaskRuntime(Protocol):
    def run_task_quantum(
        self,
        request: UserRequest,
        *,
        binding: TrustedTaskBinding,
        cancel_token: Any,
    ) -> TaskQuantumResult: ...


@dataclass(frozen=True)
class TaskQuantumResult:
    checkpoint: TaskCheckpoint
    state: AgentState
    binding: TrustedTaskBinding | None = None


class DurableTaskWorker:
    """Claim one task, execute one bounded quantum, and checkpoint it."""

    def __init__(
        self,
        *,
        service: DurableTaskService,
        runtime: AgentGraphRuntime | DurableTaskRuntime,
        worker_id: str,
        poll_seconds: float = 1.0,
    ) -> None:
        self.service = service
        self.runtime = runtime
        self.worker_id = worker_id
        self.poll_seconds = max(0.01, poll_seconds)

    def run_once(self, now: datetime | None = None) -> bool:
        lease = self.service.claim_next(worker_id=self.worker_id, now=now)
        if lease is None:
            return False
        snapshot = self.service.snapshot_for_lease(lease)
        bundle = self.service.store.load(lease.task_id)
        if bundle is None:
            return True
        binding = _binding_for_lease(lease, bundle, snapshot.ready_step_ids)
        request = _resume_request(bundle.task.user_id, bundle.task.session_id, snapshot, binding)
        try:
            result = self.runtime.run_task_quantum(
                request,
                binding=binding,
                cancel_token=self.service.task_cancel_token(lease.task_id),
            )
            if result.checkpoint.kind == "plan_revised":
                stored = self.service.store.load(lease.task_id)
                if stored is not None:
                    _release_if_still_owned(self.service, lease, stored)
                return True
            checkpoint_lease = lease
            if result.binding is not None:
                checkpoint_lease = lease.model_copy(
                    update={"task_version": result.binding.task_version}
                )
            stored = self.service.checkpoint(checkpoint_lease, result.checkpoint)
        except TaskConflict:
            return True
        except Exception as exc:
            current = self.service.store.load(lease.task_id)
            if current is not None and (
                current.task.lease_owner == lease.worker_id
                and current.task.lease_token == lease.lease_token
            ):
                lease = lease.model_copy(update={"task_version": current.task.version})
            running = next(
                (
                    run
                    for run in (current.step_runs if current is not None else [])
                    if run.plan_version == current.task.current_plan_version
                    and run.status == "running"
                ),
                None,
            )
            uncertain = running is not None and running.side_effect_level not in {
                "none",
                "local_read",
                "external_read",
            }
            try:
                stored = self.service.checkpoint(
                    lease,
                    TaskCheckpoint(
                        kind="outcome_unknown" if uncertain else "failed",
                        step_id=running.step_id if running is not None else None,
                        summary="Durable task quantum failed.",
                        error_code=(
                            "mutating_outcome_unknown"
                            if uncertain
                            else "durable_quantum_failed"
                        ),
                        error_message=str(exc),
                    ),
                )
            except TaskConflict:
                return True
        _release_if_still_owned(self.service, lease, stored)
        return True

    def run(self, stop_event: Any) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self.poll_seconds)


class DurableTaskRuntimeRouter:
    """Route explicit persisted execution profiles without intent inference."""

    def __init__(
        self,
        *,
        default_runtime: DurableTaskRuntime,
        profile_runtimes: dict[str, DurableTaskRuntime],
    ) -> None:
        self.default_runtime = default_runtime
        self.profile_runtimes = dict(profile_runtimes)

    def run_task_quantum(
        self,
        request: UserRequest,
        *,
        binding: TrustedTaskBinding,
        cancel_token: Any,
    ) -> TaskQuantumResult:
        profile = str(
            request.metadata.get("durable_execution_profile") or "agent"
        )
        runtime = self.profile_runtimes.get(profile, self.default_runtime)
        return runtime.run_task_quantum(
            request,
            binding=binding,
            cancel_token=cancel_token,
        )


def _binding_for_lease(lease: DurableTaskLease, bundle: Any, ready_step_ids: list[str]) -> TrustedTaskBinding:
    current_runs = [
        run
        for run in bundle.step_runs
        if run.plan_version == bundle.task.current_plan_version
    ]
    return TrustedTaskBinding(
        task_id=lease.task_id,
        task_version=lease.task_version,
        plan_version=bundle.task.current_plan_version,
        lease_owner=lease.worker_id,
        lease_token=lease.lease_token,
        ready_step_ids=ready_step_ids,
        step_idempotency_keys={
            run.step_id: run.idempotency_key
            for run in current_runs
            if run.step_id in ready_step_ids
        },
    )


def _resume_request(
    user_id: str,
    session_id: str,
    snapshot: Any,
    binding: TrustedTaskBinding,
) -> UserRequest:
    metadata: dict[str, Any] = {
        "durable_task_snapshot": snapshot.model_dump(mode="json"),
        "durable_task_binding": binding.model_dump(mode="json"),
        "durable_execution_profile": snapshot.execution_profile,
        "durable_workflow_payload": snapshot.workflow_payload,
        "durable_workflow_state": snapshot.workflow_state,
        "ready_tool_names": [
            step.tool_name
            for step in snapshot.plan.steps
            if step.step_id in snapshot.ready_step_ids and step.tool_name
        ],
        "_trusted_durable_execution": True,
        "conversation_history_disabled": True,
    }
    return UserRequest(
        user_id=user_id,
        session_id=session_id,
        text=snapshot.objective,
        task_execution_mode="durable",
        metadata=metadata,
    )


def _release_if_still_owned(
    service: DurableTaskService,
    lease: DurableTaskLease,
    bundle: Any,
) -> None:
    if (
        bundle.task.lease_owner == lease.worker_id
        and bundle.task.lease_token == lease.lease_token
    ):
        try:
            service.store.release(lease, expected_version=bundle.task.version)
        except TaskLeaseConflict:
            return
