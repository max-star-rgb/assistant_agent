from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

import assistant_agent.api.routes_eval_experiments as eval_routes
from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET


class FakeProcess:
    pid = 7311
    stderr = None


def _body(
    *,
    dataset: str = RUNTIME_REGRESSION_DATASET,
    run_name: str = "ui-run-1",
) -> bytes:
    return json.dumps(
        {
            "projectId": "project-sentinel",
            "datasetId": "dataset-sentinel",
            "datasetName": dataset,
            "payload": json.dumps({"runName": run_name}),
        },
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes, *, timestamp: int = 1000) -> str:
    digest = hmac.new(
        b"runtime-secret-sentinel",
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _launcher(
    tmp_path: Path,
    commands: list[list[str]],
    preflight_commands: list[list[str]],
):
    from assistant_agent.evaluation.runtime_regression import (
        RuntimeRegressionLauncher,
        RuntimeRegressionSettings,
    )

    def run(command, **kwargs):
        preflight_commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"action":"preflight","status":"ready"}',
            stderr="",
        )

    def popen(command, **kwargs):
        commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        return FakeProcess()

    return RuntimeRegressionLauncher(
        RuntimeRegressionSettings(
            enabled=True,
            signing_secret="runtime-secret-sentinel",
            runtime_ready=True,
            repository_root=Path(__file__).resolve().parents[3],
            artifact_root=tmp_path,
        ),
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "real"},
        run_factory=run,
        popen_factory=popen,
        now=lambda: 1000,
        reaper=lambda *args: None,
    )


def test_signed_webhook_preflights_and_launches_fixed_runtime_experiment(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    preflight_commands: list[list[str]] = []
    launcher = _launcher(tmp_path, commands, preflight_commands)
    body = _body()

    accepted = launcher.launch(raw_body=body, signature_header=_signature(body))

    script = str(
        Path(__file__).resolve().parents[3] / "scripts" / "run_runtime_regressions.py"
    )
    assert accepted.dataset_name == RUNTIME_REGRESSION_DATASET
    assert accepted.run_name == "ui-run-1"
    assert preflight_commands == [
        [
            sys.executable,
            script,
            "--preflight",
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]
    ]
    assert commands == [
        [
            sys.executable,
            script,
            "--run",
            "--run-name",
            "ui-run-1",
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]
    ]


def test_runtime_webhook_rejects_wrong_dataset_bad_signature_and_duplicate_delivery(
    tmp_path: Path,
) -> None:
    from assistant_agent.evaluation.runtime_regression import (
        RuntimeRegressionInvalid,
        RuntimeRegressionUnauthorized,
    )

    commands: list[list[str]] = []
    launcher = _launcher(tmp_path, commands, [])
    body = _body()

    with pytest.raises(RuntimeRegressionUnauthorized):
        launcher.launch(raw_body=body, signature_header=_signature(body, timestamp=1))

    wrong = _body(dataset="assistant-agent-runtime-regressions-0811")
    with pytest.raises(RuntimeRegressionInvalid, match="Dataset"):
        launcher.launch(raw_body=wrong, signature_header=_signature(wrong))

    first = launcher.launch(raw_body=body, signature_header=_signature(body))
    duplicate = launcher.launch(raw_body=body, signature_header=_signature(body))
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(commands) == 1


def test_runtime_webhook_api_maps_invalid_request_and_exposes_dedicated_path() -> None:
    from assistant_agent.evaluation.runtime_regression import RuntimeRegressionInvalid

    class FakeRequest:
        headers = {}

        async def body(self) -> bytes:
            return b"{}"

    class InvalidLauncher:
        def launch(self, **kwargs):
            raise RuntimeRegressionInvalid("invalid runtime regression trigger")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(eval_routes.trigger_runtime_regression(FakeRequest(), InvalidLauncher()))

    assert raised.value.status_code == 422
    assert "/internal/evals/langfuse/runtime-regression" in {
        route.path for route in eval_routes.router.routes
    }


def test_router_exposes_runtime_regression_remote_experiment_path() -> None:
    assert "/internal/evals/langfuse/runtime-regression" in {
        route.path for route in eval_routes.router.routes
    }
