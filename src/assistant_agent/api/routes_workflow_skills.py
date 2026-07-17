"""Explicit workflow skill HTTP API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.api import routes_agent
from assistant_agent.api.auth import get_auth_context, require_auth_bound_identity
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.services.api_identity import (
    ApiIdentitySource,
    AuthContext,
    IdentityPolicyError,
    enforce_identity_policy,
    resolve_request_identity,
)
from assistant_agent.services.tool_workflow_skill import WorkflowSkillRunSummary
from assistant_agent.services.tool_workflow_skill_runtime_app import (
    WorkflowSkillInfo,
    WorkflowSkillOperationResult,
    WorkflowSkillRuntimeApp,
    WorkflowSkillRuntimeIssue,
)


router = APIRouter(tags=["workflow-skills"])


class WorkflowSkillRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=10_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=200)


class WorkflowSkillResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=10_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)


class WorkflowSkillListResponse(BaseModel):
    enabled: bool
    workflows: list[WorkflowSkillInfo] = Field(default_factory=list)
    issues: list[WorkflowSkillRuntimeIssue] = Field(default_factory=list)


class WorkflowSkillSummaryResponse(BaseModel):
    summary: WorkflowSkillRunSummary


class WorkflowSkillRunListResponse(BaseModel):
    workflow_id: str = Field(min_length=1)
    summaries: list[WorkflowSkillRunSummary] = Field(default_factory=list)


def get_workflow_skill_app(request: Request) -> WorkflowSkillRuntimeApp:
    configured = getattr(request.app.state, "workflow_skill_app", None)
    if isinstance(configured, WorkflowSkillRuntimeApp):
        return configured
    return WorkflowSkillRuntimeApp.disabled()


def require_workflow_skill_app(
    runtime_app: WorkflowSkillRuntimeApp = Depends(get_workflow_skill_app),
) -> WorkflowSkillRuntimeApp:
    if not runtime_app.enabled:
        raise _http_error(
            503,
            "WORKFLOW_SKILLS_DISABLED",
            "Workflow skills are disabled.",
            recoverable=True,
        )
    return runtime_app


def workflow_request_identity(
    auth_context: AuthContext = Depends(get_auth_context),
    user_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    session_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> RequestIdentity:
    return _resolve_workflow_identity(
        auth_context=auth_context,
        user_id=user_id,
        session_id=session_id,
        source="query",
    )


@router.get("/workflow-skills", response_model=WorkflowSkillListResponse)
def list_workflow_skills(
    runtime_app: WorkflowSkillRuntimeApp = Depends(require_workflow_skill_app),
    identity: RequestIdentity = Depends(workflow_request_identity),
) -> WorkflowSkillListResponse:
    _ = identity
    return WorkflowSkillListResponse(
        enabled=runtime_app.enabled,
        workflows=runtime_app.list_workflows(),
        issues=runtime_app.issues,
    )


@router.post(
    "/workflow-skills/{workflow_id}/runs",
    response_model=WorkflowSkillOperationResult,
)
def launch_workflow_skill(
    workflow_id: str,
    body: WorkflowSkillRunRequest,
    runtime_app: WorkflowSkillRuntimeApp = Depends(require_workflow_skill_app),
    auth_context: AuthContext = Depends(get_auth_context),
) -> WorkflowSkillOperationResult:
    if not runtime_app.has_workflow(workflow_id):
        raise _workflow_not_found(workflow_id)
    if body.run_id and runtime_app.has_run(body.run_id):
        raise _workflow_run_conflict(body.run_id)
    identity = _resolve_workflow_identity(
        auth_context=auth_context,
        user_id=body.user_id,
        session_id=body.session_id,
        source="request_body",
    )
    return runtime_app.launch(
        workflow_id,
        text=body.text,
        user_id=identity.user_id,
        session_id=identity.session_id or "workflow-api",
        run_id=body.run_id,
    )


@router.post(
    "/workflow-skill-runs/{run_id}/resume",
    response_model=WorkflowSkillOperationResult,
)
def resume_workflow_skill_run(
    run_id: str,
    body: WorkflowSkillResumeRequest,
    runtime_app: WorkflowSkillRuntimeApp = Depends(require_workflow_skill_app),
    auth_context: AuthContext = Depends(get_auth_context),
) -> WorkflowSkillOperationResult:
    if runtime_app.summary(run_id) is None:
        raise _workflow_run_not_found(run_id)
    identity = _resolve_workflow_identity(
        auth_context=auth_context,
        user_id=body.user_id,
        session_id=body.session_id,
        source="request_body",
    )
    return runtime_app.resume(
        run_id,
        text=body.text,
        user_id=identity.user_id,
        session_id=identity.session_id or "workflow-api",
    )


@router.get("/workflow-skill-runs/{run_id}", response_model=WorkflowSkillSummaryResponse)
def get_workflow_skill_run(
    run_id: str,
    runtime_app: WorkflowSkillRuntimeApp = Depends(require_workflow_skill_app),
    identity: RequestIdentity = Depends(workflow_request_identity),
) -> WorkflowSkillSummaryResponse:
    _ = identity
    summary = runtime_app.summary(run_id)
    if summary is None:
        raise _workflow_run_not_found(run_id)
    return WorkflowSkillSummaryResponse(summary=summary)


@router.get(
    "/workflow-skills/{workflow_id}/runs",
    response_model=WorkflowSkillRunListResponse,
)
def list_workflow_skill_runs(
    workflow_id: str,
    runtime_app: WorkflowSkillRuntimeApp = Depends(require_workflow_skill_app),
    identity: RequestIdentity = Depends(workflow_request_identity),
) -> WorkflowSkillRunListResponse:
    _ = identity
    if not runtime_app.has_workflow(workflow_id):
        raise _workflow_not_found(workflow_id)
    return WorkflowSkillRunListResponse(
        workflow_id=workflow_id,
        summaries=runtime_app.list_run_summaries(workflow_id),
    )


def _workflow_not_found(workflow_id: str) -> HTTPException:
    return _http_error(
        404,
        "WORKFLOW_NOT_FOUND",
        "Workflow skill was not found.",
        detail={"workflow_id": workflow_id},
    )


def _workflow_run_not_found(run_id: str) -> HTTPException:
    return _http_error(
        404,
        "WORKFLOW_RUN_NOT_FOUND",
        "Workflow skill run was not found.",
        detail={"run_id": run_id},
    )


def _workflow_run_conflict(run_id: str) -> HTTPException:
    return _http_error(
        409,
        "WORKFLOW_RUN_CONFLICT",
        "Workflow skill run id already exists.",
        detail={"run_id": run_id},
    )


def _resolve_workflow_identity(
    *,
    auth_context: AuthContext,
    user_id: str | None,
    session_id: str | None,
    source: ApiIdentitySource,
) -> RequestIdentity:
    requested_user_id = user_id or auth_context.user_id or "workflow-api"
    requested_session_id = session_id or auth_context.session_id or "workflow-api"
    try:
        resolution = resolve_request_identity(
            user_id=requested_user_id,
            session_id=requested_session_id,
            source=source,
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


def _http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    detail: dict[str, str] | None = None,
    recoverable: bool = False,
) -> HTTPException:
    payload: dict[str, object] = {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }
    if detail is not None:
        payload["detail"] = detail
    return HTTPException(status_code=status_code, detail=payload)
