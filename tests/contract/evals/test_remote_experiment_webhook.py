"""Contract coverage for signed Langfuse Remote Experiment triggers."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant_agent.api.app import create_app
from assistant_agent.api.routes_eval_experiments import router
from assistant_agent.evaluation.remote_experiment import (
    DEFAULT_REMOTE_EXPERIMENT_DATASET,
    RemoteExperimentInvalid,
    RemoteExperimentLauncher,
    RemoteExperimentLaunchFailed,
    RemoteExperimentSettings,
    RemoteExperimentUnauthorized,
    _start_process_reaper,
)


SIGNING_SECRET = "test-signing-secret"
NOW = 1_800_000_000


class _FakeProcess:
    pid = 43210

    def wait(self) -> int:
        return 0


class _PopenRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> _FakeProcess:
        self.calls.append((command, kwargs))
        return _FakeProcess()


def test_assistant_app_exposes_remote_experiment_route() -> None:
    paths = {route.path for route in create_app().routes}

    assert "/internal/evals/langfuse/remote-experiment" in paths


def test_signed_webhook_launches_fixed_cli_once_and_returns_202(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    launcher = _launcher(tmp_path, recorder)
    app = FastAPI()
    app.state.remote_experiment_launcher = launcher
    app.include_router(router)
    body = _payload(
        payload_config={
            "task": "weather_timeout_recovery",
            "runName": "ui-weather-timeout",
        }
    )
    signature = _signature(body)

    with TestClient(app) as client:
        first = client.post(
            "/internal/evals/langfuse/remote-experiment",
            content=body,
            headers={
                "content-type": "application/json",
                "x-langfuse-signature": signature,
            },
        )
        duplicate = client.post(
            "/internal/evals/langfuse/remote-experiment",
            content=body,
            headers={
                "content-type": "application/json",
                "x-langfuse-signature": signature,
            },
        )

    assert first.status_code == 202
    assert first.json() == {
        "status": "accepted",
        "trigger_id": first.json()["trigger_id"],
        "duplicate": False,
        "dataset_name": DEFAULT_REMOTE_EXPERIMENT_DATASET,
        "selector_kind": "task",
        "selector_id": "weather_timeout_recovery",
        "run_name": "ui-weather-timeout",
    }
    assert duplicate.status_code == 202
    assert duplicate.json()["trigger_id"] == first.json()["trigger_id"]
    assert duplicate.json()["duplicate"] is True
    assert len(recorder.calls) == 1

    command, kwargs = recorder.calls[0]
    assert command == [
        sys.executable,
        str(tmp_path / "scripts" / "run_agent_evals.py"),
        "--run",
        "--task",
        "weather_timeout_recovery",
        "--dataset-name",
        DEFAULT_REMOTE_EXPERIMENT_DATASET,
        "--allow-real-provider",
        "--run-name",
        "ui-weather-timeout",
    ]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["MULTIMODAL_AGENT_PROVIDER_MODE"] == "real"
    receipt = json.loads(
        (
            tmp_path
            / ".data"
            / "evals"
            / "remote"
            / f"{first.json()['trigger_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["pid"] == 43210
    assert receipt["selector_id"] == "weather_timeout_recovery"


def test_empty_default_config_runs_active_dataset_items(
    tmp_path: Path,
) -> None:
    recorder = _PopenRecorder()
    launcher = _launcher(tmp_path, recorder)
    body = _payload(payload_config={})

    accepted = launcher.launch(
        raw_body=body,
        signature_header=_signature(body),
    )

    assert accepted.model_dump(mode="json") == {
        "status": "accepted",
        "trigger_id": accepted.trigger_id,
        "duplicate": False,
        "dataset_name": DEFAULT_REMOTE_EXPERIMENT_DATASET,
        "selector_kind": "dataset",
        "selector_id": DEFAULT_REMOTE_EXPERIMENT_DATASET,
        "run_name": (
            f"langfuse-ui-{DEFAULT_REMOTE_EXPERIMENT_DATASET}-"
            f"{accepted.trigger_id[:8]}"
        ),
    }
    command, _ = recorder.calls[0]
    assert command == [
        sys.executable,
        str(tmp_path / "scripts" / "run_agent_evals.py"),
        "--run",
        "--dataset-active",
        "--dataset-name",
        DEFAULT_REMOTE_EXPERIMENT_DATASET,
        "--allow-real-provider",
        "--run-name",
        accepted.run_name,
    ]


def test_webhook_rejects_invalid_or_expired_signatures(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, _PopenRecorder())
    body = _payload(payload_config={"suite": "release"})

    with pytest.raises(RemoteExperimentUnauthorized):
        launcher.launch(
            raw_body=body,
            signature_header="t=1800000000,s=00",
        )
    with pytest.raises(RemoteExperimentUnauthorized):
        launcher.launch(
            raw_body=body,
            signature_header=_signature(body, timestamp=NOW - 301),
        )


@pytest.mark.parametrize(
    ("dataset_name", "payload_config", "message"),
    [
        (
            "another-dataset",
            {"suite": "release"},
            "not allowed",
        ),
        (
            DEFAULT_REMOTE_EXPERIMENT_DATASET,
            {"task": "unknown-task"},
            "Unknown Agent eval task",
        ),
        (
            DEFAULT_REMOTE_EXPERIMENT_DATASET,
            {
                "suite": "release",
                "allowWrites": True,
            },
            "payload is invalid",
        ),
    ],
)
def test_webhook_rejects_untrusted_dataset_selector_and_options(
    tmp_path: Path,
    dataset_name: str,
    payload_config: dict[str, Any],
    message: str,
) -> None:
    recorder = _PopenRecorder()
    launcher = _launcher(tmp_path, recorder)
    payload = _payload(
        dataset_name=dataset_name,
        payload_config=payload_config,
    )

    with pytest.raises(RemoteExperimentInvalid, match=message):
        launcher.launch(
            raw_body=payload,
            signature_header=_signature(payload),
        )

    assert recorder.calls == []


def test_webhook_returns_503_until_operator_enables_real_mode(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=False)
    launcher = RemoteExperimentLauncher(
        settings,
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "mock"},
        popen_factory=_PopenRecorder(),
        now=lambda: NOW,
        reaper=lambda *_: None,
    )
    app = FastAPI()
    app.state.remote_experiment_launcher = launcher
    app.include_router(router)
    body = _payload(payload_config={"suite": "release"})

    with TestClient(app) as client:
        response = client.post(
            "/internal/evals/langfuse/remote-experiment",
            content=body,
            headers={"x-langfuse-signature": _signature(body)},
        )

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_cli_launch_failure_removes_idempotency_reservation(
    tmp_path: Path,
) -> None:
    _write_repository_contract(tmp_path)

    def fail_to_start(*_: Any, **__: Any) -> _FakeProcess:
        raise OSError("synthetic spawn failure")

    launcher = RemoteExperimentLauncher(
        _settings(tmp_path),
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "real"},
        popen_factory=fail_to_start,
        now=lambda: NOW,
        reaper=lambda *_: None,
    )
    body = _payload(payload_config={"suite": "release"})

    with pytest.raises(RemoteExperimentLaunchFailed):
        launcher.launch(
            raw_body=body,
            signature_header=_signature(body),
        )

    assert list((tmp_path / ".data" / "evals" / "remote").glob("*.json")) == []


def test_process_reaper_persists_failed_terminal_status(tmp_path: Path) -> None:
    released = threading.Event()

    class _BlockingProcess:
        pid = 54321

        def wait(self) -> int:
            assert released.wait(timeout=1.0)
            return 2

    receipt_path = tmp_path / "trigger.json"
    receipt_path.write_text('{"status":"accepted"}\n', encoding="utf-8")
    _start_process_reaper(
        _BlockingProcess(),  # type: ignore[arg-type]
        receipt_path,
        {"status": "accepted", "trigger_id": "trigger"},
    )

    released.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") == "failed":
            break
        time.sleep(0.01)

    assert receipt["status"] == "failed"
    assert receipt["exit_code"] == 2
    assert receipt["trigger_id"] == "trigger"


def _launcher(
    repository_root: Path,
    recorder: _PopenRecorder,
) -> RemoteExperimentLauncher:
    _write_repository_contract(repository_root)
    return RemoteExperimentLauncher(
        _settings(repository_root),
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "real"},
        popen_factory=recorder,
        now=lambda: NOW,
        reaper=lambda *_: None,
    )


def _settings(
    repository_root: Path,
    *,
    enabled: bool = True,
) -> RemoteExperimentSettings:
    return RemoteExperimentSettings(
        enabled=enabled,
        signing_secret=SIGNING_SECRET,
        dataset_name=DEFAULT_REMOTE_EXPERIMENT_DATASET,
        repository_root=repository_root,
        artifact_root=repository_root / ".data" / "evals" / "remote",
    )


def _write_repository_contract(repository_root: Path) -> None:
    task_root = (
        repository_root
        / "evals"
        / "agent"
        / "tasks"
        / "weather_timeout_recovery"
    )
    task_root.mkdir(parents=True, exist_ok=True)
    (task_root / "task.json").write_text("{}\n", encoding="utf-8")
    suites_path = repository_root / "evals" / "agent" / "suites.json"
    suites_path.write_text(
        json.dumps({"release": ["weather_timeout_recovery"]}),
        encoding="utf-8",
    )
    scripts_root = repository_root / "scripts"
    scripts_root.mkdir(parents=True, exist_ok=True)
    (scripts_root / "run_agent_evals.py").write_text(
        "# test entrypoint\n",
        encoding="utf-8",
    )


def _payload(
    *,
    payload_config: dict[str, Any],
    dataset_name: str = DEFAULT_REMOTE_EXPERIMENT_DATASET,
) -> bytes:
    return json.dumps(
        {
            "projectId": "project-test-id",
            "datasetId": "dataset-test-id",
            "datasetName": dataset_name,
            "payload": json.dumps(
                payload_config,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _signature(body: bytes, *, timestamp: int = NOW) -> str:
    signature = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        str(timestamp).encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={signature}"
