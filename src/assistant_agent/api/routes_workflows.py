"""Identity-scoped durable Workflow HTTP facade."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.api import routes_agent
from assistant_agent.api.auth import get_auth_context, require_auth_bound_identity
from assistant_agent.api.identity import (
    AuthContext,
    IdentityPolicyError,
    enforce_identity_policy,
    resolve_request_identity,
)
from assistant_agent.api.models import (
    WorkflowActionResponse,
    WorkflowEventsResponse,
    WorkflowResponse,
    WorkflowResultResponse,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.service import (
    WorkflowAccessDenied,
    WorkflowNotFound,
    WorkflowServiceError,
    WorkflowStateConflict,
)
from assistant_agent.workflows.store import WorkflowStoreError
from assistant_agent.workflows.graph_host import (
    WorkflowGraphHandle,
    WorkflowGraphHost,
    WorkflowGraphHostError,
)


router = APIRouter(prefix="/workflows", tags=["durable-workflows"])


class WorkflowInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_ref: str = Field(min_length=1, max_length=800)
    values: dict[str, str]


class WorkflowCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: Literal["user_requested"] = "user_requested"


def get_workflow_graph_host() -> WorkflowGraphHost:
    host = getattr(routes_agent.get_agent_runtime(), "workflow_graph_host", None)
    if not isinstance(host, WorkflowGraphHost):
        raise _http_error(503, "WORKFLOWS_DISABLED", "Durable workflows are disabled.")
    return host


def workflow_request_identity(
    auth_context: AuthContext = Depends(get_auth_context),
    user_id: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
) -> RequestIdentity:
    requested_user_id = auth_context.user_id if auth_context.authenticated else user_id
    requested_session_id = (
        auth_context.session_id if auth_context.authenticated else session_id
    )
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
        raise _http_error(
            403, "TRIAL_ACCESS_DENIED", trial.reason or "Trial access denied."
        )
    return resolution.identity


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str,
    identity: RequestIdentity = Depends(workflow_request_identity),
    host: WorkflowGraphHost = Depends(get_workflow_graph_host),
) -> WorkflowResponse:
    try:
        snapshot = await host.get_status(
            identity=identity,
            workflow_id=workflow_id,
        )
        return _graph_workflow_response(snapshot)
    except (WorkflowGraphHostError, WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc


@router.get("/{workflow_id}/events", response_model=WorkflowEventsResponse)
async def get_workflow_events(
    workflow_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    identity: RequestIdentity = Depends(workflow_request_identity),
    host: WorkflowGraphHost = Depends(get_workflow_graph_host),
) -> WorkflowEventsResponse:
    try:
        page = await host.get_events(
            identity=identity,
            workflow_id=workflow_id,
            after=after,
            limit=limit,
        )
    except (WorkflowGraphHostError, WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc
    return WorkflowEventsResponse(
        workflow_id=workflow_id,
        events=tuple(event.model_dump(mode="json") for event in page.events),
        next_cursor=page.next_cursor,
    )


@router.get("/{workflow_id}/result", response_model=WorkflowResultResponse)
async def get_workflow_result(
    workflow_id: str,
    identity: RequestIdentity = Depends(workflow_request_identity),
    host: WorkflowGraphHost = Depends(get_workflow_graph_host),
) -> WorkflowResultResponse:
    try:
        result = await host.get_result(identity=identity, workflow_id=workflow_id)
    except (WorkflowGraphHostError, WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc
    return WorkflowResultResponse(
        workflow_id=workflow_id,
        artifact_ref=result.artifact_ref,
        content=result.content,
    )


@router.post("/{workflow_id}/input", response_model=WorkflowActionResponse)
async def provide_workflow_input(
    workflow_id: str,
    body: WorkflowInputRequest,
    identity: RequestIdentity = Depends(workflow_request_identity),
    host: WorkflowGraphHost = Depends(get_workflow_graph_host),
) -> WorkflowActionResponse:
    try:
        handle = await host.resume(
            identity=identity,
            workflow_id=workflow_id,
            action_ref=body.action_ref,
            values=dict(body.values),
        )
        return WorkflowActionResponse(workflow=handle.model_dump(mode="json"))
    except (WorkflowGraphHostError, WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc


@router.post("/{workflow_id}/cancel", response_model=WorkflowActionResponse)
async def cancel_workflow(
    workflow_id: str,
    body: WorkflowCancelRequest,
    identity: RequestIdentity = Depends(workflow_request_identity),
    host: WorkflowGraphHost = Depends(get_workflow_graph_host),
) -> WorkflowActionResponse:
    try:
        handle = await host.cancel(
            identity=identity,
            workflow_id=workflow_id,
            reason_code=body.reason_code,
        )
        return WorkflowActionResponse(workflow=handle.model_dump(mode="json"))
    except (WorkflowGraphHostError, WorkflowServiceError, WorkflowStoreError) as exc:
        raise _map_error(exc) from exc


def _graph_workflow_response(
    snapshot,
) -> WorkflowResponse:
    source = snapshot.handle
    handle = WorkflowGraphHandle(
        workflow_id=source.workflow_id,
        workflow_type=source.workflow_type,
        execution_engine=getattr(source, "execution_engine", "langgraph_v3"),
        status=source.status,
        phase=source.phase,
        output_ref=source.output_ref,
    ).model_dump(mode="json")
    return WorkflowResponse(
        workflow=handle,
        progress=snapshot.progress.model_dump(mode="json"),
        result_artifact_refs=snapshot.result_artifact_refs,
        waiting_actions=tuple(
            item.model_dump(mode="json") for item in snapshot.waiting_actions
        ),
        terminal_reason_code=snapshot.terminal_reason_code,
    )


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowGraphHostError):
        if exc.code in {"workflow_not_found", "workflow_result_not_found"}:
            return _http_error(404, exc.code.upper(), str(exc))
        if exc.code == "workflow_result_not_ready":
            return _http_error(409, exc.code.upper(), str(exc))
        return _http_error(409, "WORKFLOW_CONFLICT", str(exc))
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
