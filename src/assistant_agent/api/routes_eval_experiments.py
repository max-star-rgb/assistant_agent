"""Signed Langfuse Remote Experiment HTTP entrypoint."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from assistant_agent.evaluation.release_review import (
    ReleaseReviewAccepted,
    ReleaseReviewDisabled,
    ReleaseReviewInvalid,
    ReleaseReviewLauncher,
    ReleaseReviewLaunchFailed,
    ReleaseReviewPreflightFailed,
    ReleaseReviewSettings,
    ReleaseReviewUnauthorized,
)
from assistant_agent.evaluation.runtime_regression import (
    RuntimeRegressionAccepted,
    RuntimeRegressionDisabled,
    RuntimeRegressionInvalid,
    RuntimeRegressionLauncher,
    RuntimeRegressionLaunchFailed,
    RuntimeRegressionPreflightFailed,
    RuntimeRegressionSettings,
    RuntimeRegressionUnauthorized,
)


router = APIRouter(prefix="/internal/evals", tags=["agent-evals"])


def get_release_review_launcher(request: Request) -> ReleaseReviewLauncher:
    configured = getattr(
        request.app.state,
        "release_review_launcher",
        None,
    )
    if isinstance(configured, ReleaseReviewLauncher):
        return configured
    launcher = ReleaseReviewLauncher(
        ReleaseReviewSettings.from_env(os.environ)
    )
    request.app.state.release_review_launcher = launcher
    return launcher


def get_runtime_regression_launcher(request: Request) -> RuntimeRegressionLauncher:
    configured = getattr(request.app.state, "runtime_regression_launcher", None)
    if isinstance(configured, RuntimeRegressionLauncher):
        return configured
    launcher = RuntimeRegressionLauncher(RuntimeRegressionSettings.from_env(os.environ))
    request.app.state.runtime_regression_launcher = launcher
    return launcher


@router.post(
    "/langfuse/release-review",
    response_model=ReleaseReviewAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_release_review(
    request: Request,
    launcher: ReleaseReviewLauncher = Depends(
        get_release_review_launcher
    ),
) -> ReleaseReviewAccepted:
    raw_body = await request.body()
    try:
        return await run_in_threadpool(
            launcher.launch,
            raw_body=raw_body,
            signature_header=request.headers.get("x-langfuse-signature"),
        )
    except ReleaseReviewUnauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ReleaseReviewInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ReleaseReviewDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReleaseReviewPreflightFailed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReleaseReviewLaunchFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/langfuse/runtime-regression",
    response_model=RuntimeRegressionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_runtime_regression(
    request: Request,
    launcher: RuntimeRegressionLauncher = Depends(get_runtime_regression_launcher),
) -> RuntimeRegressionAccepted:
    raw_body = await request.body()
    try:
        return await run_in_threadpool(
            launcher.launch,
            raw_body=raw_body,
            signature_header=request.headers.get("x-langfuse-signature"),
        )
    except RuntimeRegressionUnauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeRegressionInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RuntimeRegressionDisabled, RuntimeRegressionPreflightFailed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeRegressionLaunchFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
