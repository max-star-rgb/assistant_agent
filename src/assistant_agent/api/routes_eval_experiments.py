"""Signed Langfuse Remote Experiment HTTP entrypoint."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status

from assistant_agent.evaluation.release_review import (
    ReleaseReviewAccepted,
    ReleaseReviewDisabled,
    ReleaseReviewInvalid,
    ReleaseReviewLauncher,
    ReleaseReviewLaunchFailed,
    ReleaseReviewSettings,
    ReleaseReviewUnauthorized,
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
        return launcher.launch(
            raw_body=raw_body,
            signature_header=request.headers.get("x-langfuse-signature"),
        )
    except ReleaseReviewUnauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ReleaseReviewInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ReleaseReviewDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReleaseReviewLaunchFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
