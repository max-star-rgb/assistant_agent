"""Explicit skill HTTP API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.api import routes_agent
from assistant_agent.api.auth import get_auth_context, require_auth_bound_identity
from assistant_agent.identity import RequestIdentity
from assistant_agent.api.identity import (
    ApiIdentitySource,
    AuthContext,
    IdentityPolicyError,
    enforce_identity_policy,
    resolve_request_identity,
)
from assistant_agent.skills.runtime import SkillRunSummary
from assistant_agent.skills.application import (
    SkillInfo,
    SkillOperationResult,
    SkillRuntimeApp,
    SkillRuntimeIssue,
)


router = APIRouter(tags=["skills"])


class SkillRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=10_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=200)


class SkillResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=10_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)


class SkillListResponse(BaseModel):
    enabled: bool
    skills: list[SkillInfo] = Field(default_factory=list)
    issues: list[SkillRuntimeIssue] = Field(default_factory=list)


class SkillSummaryResponse(BaseModel):
    summary: SkillRunSummary


class SkillRunListResponse(BaseModel):
    skill_id: str = Field(min_length=1)
    summaries: list[SkillRunSummary] = Field(default_factory=list)


def get_skill_app(request: Request) -> SkillRuntimeApp:
    configured = getattr(request.app.state, "skill_app", None)
    if isinstance(configured, SkillRuntimeApp):
        return configured
    return SkillRuntimeApp.disabled()


def require_skill_app(
    runtime_app: SkillRuntimeApp = Depends(get_skill_app),
) -> SkillRuntimeApp:
    if not runtime_app.enabled:
        raise _http_error(
            503,
            "SKILLS_DISABLED",
            "Skills are disabled.",
            recoverable=True,
        )
    return runtime_app


def skill_request_identity(
    auth_context: AuthContext = Depends(get_auth_context),
    user_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    session_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> RequestIdentity:
    return _resolve_skill_identity(
        auth_context=auth_context,
        user_id=user_id,
        session_id=session_id,
        source="query",
    )


@router.get("/skills", response_model=SkillListResponse)
def list_skills(
    runtime_app: SkillRuntimeApp = Depends(require_skill_app),
    identity: RequestIdentity = Depends(skill_request_identity),
) -> SkillListResponse:
    _ = identity
    return SkillListResponse(
        enabled=runtime_app.enabled,
        skills=runtime_app.list_skills(),
        issues=runtime_app.issues,
    )


@router.post(
    "/skills/{skill_id}/runs",
    response_model=SkillOperationResult,
)
def launch_skill(
    skill_id: str,
    body: SkillRunRequest,
    runtime_app: SkillRuntimeApp = Depends(require_skill_app),
    auth_context: AuthContext = Depends(get_auth_context),
) -> SkillOperationResult:
    if not runtime_app.has_skill(skill_id):
        raise _skill_not_found(skill_id)
    if body.run_id and runtime_app.has_run(body.run_id):
        raise _skill_run_conflict(body.run_id)
    identity = _resolve_skill_identity(
        auth_context=auth_context,
        user_id=body.user_id,
        session_id=body.session_id,
        source="request_body",
    )
    return runtime_app.launch(
        skill_id,
        text=body.text,
        user_id=identity.user_id,
        session_id=identity.session_id or "skill-api",
        run_id=body.run_id,
    )


@router.post(
    "/skill-runs/{run_id}/resume",
    response_model=SkillOperationResult,
)
def resume_skill_run(
    run_id: str,
    body: SkillResumeRequest,
    runtime_app: SkillRuntimeApp = Depends(require_skill_app),
    auth_context: AuthContext = Depends(get_auth_context),
) -> SkillOperationResult:
    if runtime_app.summary(run_id) is None:
        raise _skill_run_not_found(run_id)
    identity = _resolve_skill_identity(
        auth_context=auth_context,
        user_id=body.user_id,
        session_id=body.session_id,
        source="request_body",
    )
    return runtime_app.resume(
        run_id,
        text=body.text,
        user_id=identity.user_id,
        session_id=identity.session_id or "skill-api",
    )


@router.get("/skill-runs/{run_id}", response_model=SkillSummaryResponse)
def get_skill_run(
    run_id: str,
    runtime_app: SkillRuntimeApp = Depends(require_skill_app),
    identity: RequestIdentity = Depends(skill_request_identity),
) -> SkillSummaryResponse:
    _ = identity
    summary = runtime_app.summary(run_id)
    if summary is None:
        raise _skill_run_not_found(run_id)
    return SkillSummaryResponse(summary=summary)


@router.get(
    "/skills/{skill_id}/runs",
    response_model=SkillRunListResponse,
)
def list_skill_runs(
    skill_id: str,
    runtime_app: SkillRuntimeApp = Depends(require_skill_app),
    identity: RequestIdentity = Depends(skill_request_identity),
) -> SkillRunListResponse:
    _ = identity
    if not runtime_app.has_skill(skill_id):
        raise _skill_not_found(skill_id)
    return SkillRunListResponse(
        skill_id=skill_id,
        summaries=runtime_app.list_run_summaries(skill_id),
    )


def _skill_not_found(skill_id: str) -> HTTPException:
    return _http_error(
        404,
        "SKILL_NOT_FOUND",
        "Skill was not found.",
        detail={"skill_id": skill_id},
    )


def _skill_run_not_found(run_id: str) -> HTTPException:
    return _http_error(
        404,
        "SKILL_RUN_NOT_FOUND",
        "Skill run was not found.",
        detail={"run_id": run_id},
    )


def _skill_run_conflict(run_id: str) -> HTTPException:
    return _http_error(
        409,
        "SKILL_RUN_CONFLICT",
        "Skill run id already exists.",
        detail={"run_id": run_id},
    )


def _resolve_skill_identity(
    *,
    auth_context: AuthContext,
    user_id: str | None,
    session_id: str | None,
    source: ApiIdentitySource,
) -> RequestIdentity:
    requested_user_id = user_id or auth_context.user_id or "skill-api"
    requested_session_id = session_id or auth_context.session_id or "skill-api"
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
