"""Identity-scoped durable task HTTP API."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.api import routes_agent
from assistant_agent.api.auth import get_auth_context, require_auth_bound_identity
from assistant_agent.api.models import DurableTaskEventsResponse, DurableTaskResponse
from assistant_agent.identity import RequestIdentity
from assistant_agent.api.identity import (
    AuthContext,
    IdentityPolicyError,
    enforce_identity_policy,
    resolve_request_identity,
)
from assistant_agent.automation.durable_tasks.service import (
    DurableTaskError,
    DurableTaskService,
    TaskAccessDenied,
    TaskConflict,
    TaskNotFound,
    TaskTransitionRejected,
)
from assistant_agent.automation.durable_tasks.store import TaskStoreError


router = APIRouter(prefix="/tasks", tags=["durable-tasks"])


class TaskInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10_000)


class TaskCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="user_requested", min_length=1, max_length=1_000)


def get_durable_task_service() -> DurableTaskService:
    service = getattr(routes_agent.get_agent_runtime(), "durable_task_service", None)
    if not isinstance(service, DurableTaskService):
        raise _http_error(503, "DURABLE_TASKS_DISABLED", "Durable tasks are disabled.")
    return service


def task_request_identity(
    auth_context: AuthContext = Depends(get_auth_context),
    user_id: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
) -> RequestIdentity:
    requested_user_id = auth_context.user_id if auth_context.authenticated else user_id
    requested_session_id = auth_context.session_id if auth_context.authenticated else session_id
    try:
        resolution = resolve_request_identity(
            user_id=requested_user_id or "",
            session_id=requested_session_id,
            source="auth_context" if auth_context.authenticated else "query",
            auth_context=auth_context,
        )
        enforce_identity_policy(
            resolution,
            production_required=require_auth_bound_identity(),
        )
    except (ValueError, IdentityPolicyError) as exc:
        detail = exc.detail() if isinstance(exc, IdentityPolicyError) else str(exc)
        raise HTTPException(status_code=403, detail=detail) from exc
    trial = resolution.trial_access(routes_agent.get_trial_access_gate())
    if not trial.allowed:
        raise _http_error(403, "TRIAL_ACCESS_DENIED", trial.reason or "Trial access denied.")
    return resolution.identity


@router.get("/{task_id}", response_model=DurableTaskResponse)
def get_task(
    task_id: str,
    identity: RequestIdentity = Depends(task_request_identity),
    service: DurableTaskService = Depends(get_durable_task_service),
) -> DurableTaskResponse:
    try:
        return _task_response(service.get_task(identity=identity, task_id=task_id))
    except (DurableTaskError, TaskStoreError) as exc:
        raise _map_task_error(exc) from exc


@router.get("/{task_id}/events", response_model=DurableTaskEventsResponse)
def get_task_events(
    task_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    identity: RequestIdentity = Depends(task_request_identity),
    service: DurableTaskService = Depends(get_durable_task_service),
) -> DurableTaskEventsResponse:
    try:
        events = service.list_events(
            identity=identity,
            task_id=task_id,
            after=after,
            limit=limit,
        )
    except (DurableTaskError, TaskStoreError) as exc:
        raise _map_task_error(exc) from exc
    payload = [event.model_dump(mode="json") for event in events]
    return DurableTaskEventsResponse(
        task_id=task_id,
        events=payload,
        next_cursor=events[-1].cursor if events else after,
    )


@router.post("/{task_id}/input", response_model=DurableTaskResponse)
def provide_task_input(
    task_id: str,
    body: TaskInputRequest,
    identity: RequestIdentity = Depends(task_request_identity),
    service: DurableTaskService = Depends(get_durable_task_service),
) -> DurableTaskResponse:
    try:
        return _task_response(
            service.provide_input(identity=identity, task_id=task_id, text=body.text)
        )
    except (DurableTaskError, TaskStoreError) as exc:
        raise _map_task_error(exc) from exc


@router.post("/{task_id}/cancel", response_model=DurableTaskResponse)
def cancel_task(
    task_id: str,
    body: TaskCancelRequest,
    identity: RequestIdentity = Depends(task_request_identity),
    service: DurableTaskService = Depends(get_durable_task_service),
) -> DurableTaskResponse:
    try:
        return _task_response(
            service.cancel(identity=identity, task_id=task_id, reason=body.reason)
        )
    except (DurableTaskError, TaskStoreError) as exc:
        raise _map_task_error(exc) from exc


def _task_response(bundle: Any) -> DurableTaskResponse:
    plan = next(
        item
        for item in bundle.plans
        if item.plan_version == bundle.task.current_plan_version
    )
    steps = [
        {
            "step_id": run.step_id,
            "plan_version": run.plan_version,
            "tool_name": run.tool_name,
            "status": run.status,
            "attempt": run.attempt,
            "summary": run.summary,
            "output_ref": run.output_ref,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }
        for run in bundle.step_runs
        if run.plan_version == bundle.task.current_plan_version
    ]
    return DurableTaskResponse(
        task={
            "task_id": bundle.task.task_id,
            "objective": bundle.task.objective,
            "active_constraints": list(bundle.task.active_constraints),
            "status": bundle.task.status,
            "plan_version": bundle.task.current_plan_version,
            "remaining_budget": dict(bundle.task.remaining_budget),
            "created_at": bundle.task.created_at,
            "updated_at": bundle.task.updated_at,
            "started_at": bundle.task.started_at,
            "terminal_at": bundle.task.terminal_at,
            "wait": (
                bundle.task.wait.model_dump(mode="json")
                if bundle.task.wait is not None
                else None
            ),
        },
        plan=plan.plan.model_dump(mode="json"),
        steps=steps,
        artifacts=[item.model_dump(mode="json") for item in bundle.artifacts],
    )


def _map_task_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (TaskNotFound, TaskAccessDenied)):
        return _http_error(404, "TASK_NOT_FOUND", "Task not found.")
    if isinstance(exc, TaskTransitionRejected):
        return _http_error(409, "TASK_TERMINAL", str(exc))
    if isinstance(exc, TaskConflict):
        return _http_error(409, "TASK_CONFLICT", str(exc))
    return _http_error(409, "TASK_CONFLICT", "Task operation failed.")


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "recoverable": status_code < 500},
    )
