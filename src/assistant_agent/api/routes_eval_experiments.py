"""Signed Langfuse Remote Experiment HTTP entrypoint."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status

from assistant_agent.evaluation.remote_experiment import (
    RemoteExperimentAccepted,
    RemoteExperimentDisabled,
    RemoteExperimentInvalid,
    RemoteExperimentLauncher,
    RemoteExperimentLaunchFailed,
    RemoteExperimentSettings,
    RemoteExperimentUnauthorized,
)


router = APIRouter(prefix="/internal/evals", tags=["agent-evals"])


def get_remote_experiment_launcher(request: Request) -> RemoteExperimentLauncher:
    configured = getattr(
        request.app.state,
        "remote_experiment_launcher",
        None,
    )
    if isinstance(configured, RemoteExperimentLauncher):
        return configured
    launcher = RemoteExperimentLauncher(
        RemoteExperimentSettings.from_env(os.environ)
    )
    request.app.state.remote_experiment_launcher = launcher
    return launcher


@router.post(
    "/langfuse/remote-experiment",
    response_model=RemoteExperimentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_remote_experiment(
    request: Request,
    launcher: RemoteExperimentLauncher = Depends(
        get_remote_experiment_launcher
    ),
) -> RemoteExperimentAccepted:
    raw_body = await request.body()
    try:
        return launcher.launch(
            raw_body=raw_body,
            signature_header=request.headers.get("x-langfuse-signature"),
        )
    except RemoteExperimentUnauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RemoteExperimentInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteExperimentDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RemoteExperimentLaunchFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
