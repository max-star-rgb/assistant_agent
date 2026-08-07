"""Identity-scoped durable Workflow HTTP facade."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from assistant_agent.api import routes_agent
from assistant_agent.api.auth import get_auth_context, require_auth_bound_identity
from assistant_agent.api.identity import (
    AuthContext,
    IdentityPolicyError,
    enforce_identity_policy,
    resolve_request_identity,
)
from assistant_agent.api.models import WorkflowEventsResponse, WorkflowResponse
from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.service import (
    WorkflowAccessDenied,
    WorkflowNotFound,
    WorkflowService,
    WorkflowServiceError,
    WorkflowStateConflict,
)
from assistant_agent.workflows.store import WorkflowStoreError


router = APIRouter(prefix="/workflows", tags=["durable-workflows"])


class WorkflowInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume_token: str = Field(min_length=1, max_length=240)
    values: dict[str, JsonValue]


class WorkflowCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: str = Field(default="user_requested", min_length=1, max_length=160)


def get_workflow_service() -> WorkflowService:
    service = getattr(routes_agent.get_agent_runtime(), "workflow_service", None)
    if not isinstance(service, WorkflowService):
        raise _http_error(503, "WORKFLOWS_DISABLED", "Durable workflows are disabled.")
    return service


def workflow_request_identity(
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


@router.get("/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(
    workflow_id: str,
    identity: RequestIdentity = Depends(workflow_request_identity),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowResponse:
    try:
        return _workflow_response(
            service.get_workflow(identity=identity, workflow_id=workflow_id)
        )
    except (WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc


@router.get("/{workflow_id}/events", response_model=WorkflowEventsResponse)
def get_workflow_events(
    workflow_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    identity: RequestIdentity = Depends(workflow_request_identity),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowEventsResponse:
    try:
        events = service.list_events(
            identity=identity,
            workflow_id=workflow_id,
            after=after,
            limit=limit,
        )
    except (WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc
    return WorkflowEventsResponse(
        workflow_id=workflow_id,
        events=[event.model_dump(mode="json") for event in events],
        next_cursor=events[-1].cursor if events else after,
    )


@router.post("/{workflow_id}/input", response_model=WorkflowResponse)
def provide_workflow_input(
    workflow_id: str,
    body: WorkflowInputRequest,
    identity: RequestIdentity = Depends(workflow_request_identity),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowResponse:
    try:
        return _workflow_response(service.provide_input(
            identity=identity,
            workflow_id=workflow_id,
            resume_token=body.resume_token,
            values=dict(body.values),
        ))
    except (WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc


@router.post("/{workflow_id}/cancel", response_model=WorkflowResponse)
def cancel_workflow(
    workflow_id: str,
    body: WorkflowCancelRequest,
    identity: RequestIdentity = Depends(workflow_request_identity),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowResponse:
    try:
        return _workflow_response(service.cancel(
            identity=identity,
            workflow_id=workflow_id,
            reason_code=body.reason_code,
        ))
    except (WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc


def _workflow_response(bundle) -> WorkflowResponse:
    workflow = bundle.workflow
    return WorkflowResponse(
        workflow={
            "workflow_id": workflow.workflow_id,
            "workflow_type": workflow.workflow_type,
            "status": workflow.status,
            "phase": workflow.phase,
            "revision": workflow.revision,
            "objective": workflow.objective,
            "deliverables": list(workflow.deliverables),
            "remaining_budget": workflow.budget.model_dump(mode="json"),
            "waiting_input": workflow.waiting_input,
            "result_artifact_refs": list(workflow.result_artifact_refs),
            "created_at": workflow.created_at,
            "updated_at": workflow.updated_at,
            "terminal_at": workflow.terminal_at,
        },
        plan=bundle.current_plan.model_dump(mode="json"),
    )


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (WorkflowNotFound, WorkflowAccessDenied)):
        return _http_error(404, "WORKFLOW_NOT_FOUND", "Workflow not found.")
    if isinstance(exc, WorkflowStateConflict):
        return _http_error(409, "WORKFLOW_CONFLICT", str(exc))
    return _http_error(409, "WORKFLOW_CONFLICT", "Workflow operation failed.")


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "recoverable": status_code < 500},
    )
