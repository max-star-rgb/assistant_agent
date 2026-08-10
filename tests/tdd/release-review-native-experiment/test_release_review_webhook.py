from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

import assistant_agent.evaluation.release_review as release_review_module
from assistant_agent.evaluation.release_review import (
    RELEASE_REVIEW_DATASET,
    ReleaseReviewInvalid,
    ReleaseReviewLauncher,
    ReleaseReviewLaunchFailed,
    ReleaseReviewSettings,
    ReleaseReviewUnauthorized,
)
from assistant_agent.api.routes_eval_experiments import (
    router,
    trigger_release_review,
)


class FakeProcess:
    pid = 4321
    stderr = None


def _body(payload: dict, *, dataset: str = RELEASE_REVIEW_DATASET) -> bytes:
    return json.dumps(
        {
            "projectId": "project-sentinel",
            "datasetId": "dataset-sentinel",
            "datasetName": dataset,
            "payload": json.dumps(payload),
        },
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes, *, secret: str = "secret-sentinel", timestamp: int = 1000) -> str:
    digest = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _launcher(
    tmp_path: Path,
    commands: list[list[str]],
    *,
    preflight_commands: list[list[str]] | None = None,
    preflight_returncode: int = 0,
    run_factory=None,
) -> ReleaseReviewLauncher:
    def popen(command, **kwargs):
        commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        return FakeProcess()

    def run(command, **kwargs):
        if preflight_commands is not None:
            preflight_commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 30
        return __import__("subprocess").CompletedProcess(
            command,
            preflight_returncode,
            stdout=(
                '{"action":"preflight","status":"ready"}'
                if preflight_returncode == 0
                else '{"error":"release_review_infrastructure_failure",'
                '"message":"missing required tools: weather"}'
            ),
            stderr="",
        )

    return ReleaseReviewLauncher(
        ReleaseReviewSettings(
            enabled=True,
            signing_secret="secret-sentinel",
            staging_ready=True,
            repository_root=Path(__file__).resolve().parents[3],
            artifact_root=tmp_path,
        ),
        env={"MULTIMODAL_AGENT_PROVIDER_MODE": "real"},
        popen_factory=popen,
        run_factory=run_factory or run,
        now=lambda: 1000,
        reaper=lambda *args: None,
    )


def test_signed_webhook_launches_only_fixed_release_review_argv(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    preflight_commands: list[list[str]] = []
    launcher = _launcher(
        tmp_path,
        commands,
        preflight_commands=preflight_commands,
    )
    body = _body(
        {
            "releaseId": "release-1",
            "scenarios": ["deep_research_admission", "simple_request_no_workflow"],
            "runName": "run-1",
        }
    )

    accepted = launcher.launch(
        raw_body=body,
        signature_header=_signature(body),
    )

    assert accepted.dataset_name == RELEASE_REVIEW_DATASET
    assert accepted.release_id == "release-1"
    assert preflight_commands == [
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "scripts" / "run_release_review.py"),
            "--preflight",
            "--release-id",
            "release-1",
            "--allow-real-provider",
            "--allow-staging-side-effects",
            "--scenario",
            "deep_research_admission",
            "--scenario",
            "simple_request_no_workflow",
        ]
    ]
    assert commands == [
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "scripts" / "run_release_review.py"),
            "--run",
            "--release-id",
            "release-1",
            "--allow-real-provider",
            "--allow-staging-side-effects",
            "--scenario",
            "deep_research_admission",
            "--scenario",
            "simple_request_no_workflow",
            "--run-name",
            "run-1",
        ]
    ]


def test_preflight_failure_is_returned_before_async_process_launch(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    launcher = _launcher(
        tmp_path,
        commands,
        preflight_returncode=2,
    )
    body = _body({"releaseId": "release-1"})

    with pytest.raises(
        ReleaseReviewLaunchFailed,
        match="missing required tools: weather",
    ):
        launcher.launch(raw_body=body, signature_header=_signature(body))

    assert commands == []


def test_preflight_failure_does_not_create_cross_launcher_duplicate_acceptance(
    tmp_path: Path,
) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    first_error: list[Exception] = []
    first_commands: list[list[str]] = []
    second_commands: list[list[str]] = []

    def failing_preflight(command, **kwargs):
        first_started.set()
        assert release_first.wait(timeout=2)
        return __import__("subprocess").CompletedProcess(
            command,
            2,
            stdout='{"message":"catalog unavailable"}',
            stderr="",
        )

    first = _launcher(
        tmp_path,
        first_commands,
        run_factory=failing_preflight,
    )
    second = _launcher(tmp_path, second_commands)
    body = _body({"releaseId": "release-1"})
    signature = _signature(body)

    def launch_first() -> None:
        try:
            first.launch(raw_body=body, signature_header=signature)
        except Exception as exc:
            first_error.append(exc)

    thread = threading.Thread(target=launch_first)
    thread.start()
    assert first_started.wait(timeout=2)
    accepted = second.launch(raw_body=body, signature_header=signature)
    release_first.set()
    thread.join(timeout=2)

    assert accepted.duplicate is False
    assert len(second_commands) == 1
    assert len(first_error) == 1
    assert isinstance(first_error[0], ReleaseReviewLaunchFailed)
    assert (tmp_path / f"{accepted.trigger_id}.json").is_file()


def test_api_maps_preflight_readiness_failure_to_service_unavailable() -> None:
    preflight_error_type = getattr(
        release_review_module,
        "ReleaseReviewPreflightFailed",
        ReleaseReviewLaunchFailed,
    )

    class FakeRequest:
        headers = {}

        async def body(self) -> bytes:
            return b"{}"

    class FailingLauncher:
        def launch(self, **kwargs):
            raise preflight_error_type("catalog unavailable")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(trigger_release_review(FakeRequest(), FailingLauncher()))

    assert raised.value.status_code == 503
    assert raised.value.detail == "catalog unavailable"


def test_signature_expiry_wrong_dataset_and_extra_payload_are_rejected(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path, [])
    payload = {"releaseId": "release-1"}
    body = _body(payload)

    with pytest.raises(ReleaseReviewUnauthorized):
        launcher.launch(raw_body=body, signature_header=_signature(body, timestamp=1))
    wrong_dataset = _body(payload, dataset="assistant-agent-regression")
    with pytest.raises(ReleaseReviewInvalid, match="Dataset"):
        launcher.launch(
            raw_body=wrong_dataset,
            signature_header=_signature(wrong_dataset),
        )
    extra = _body({**payload, "shell": "touch /tmp/not-allowed"})
    with pytest.raises(ReleaseReviewInvalid, match="payload"):
        launcher.launch(raw_body=extra, signature_header=_signature(extra))


@pytest.mark.parametrize("field", ("model", "promptVersion"))
def test_payload_rejects_server_owned_model_and_manual_prompt_version(
    tmp_path: Path, field: str
) -> None:
    launcher = _launcher(tmp_path, [])
    body = _body({"releaseId": "release-1", field: "obsolete-value"})

    with pytest.raises(ReleaseReviewInvalid, match="payload"):
        launcher.launch(raw_body=body, signature_header=_signature(body))


def test_duplicate_delivery_is_idempotent_and_identifiers_are_safe(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    launcher = _launcher(tmp_path, commands)
    body = _body({"releaseId": "release-1"})
    signature = _signature(body)

    first = launcher.launch(raw_body=body, signature_header=signature)
    duplicate = launcher.launch(raw_body=body, signature_header=signature)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(commands) == 1

    unsafe = _body({"releaseId": "../production"})
    with pytest.raises(ReleaseReviewInvalid):
        launcher.launch(raw_body=unsafe, signature_header=_signature(unsafe))


def test_api_exposes_only_the_new_release_review_path() -> None:
    paths = {route.path for route in router.routes}

    assert "/internal/evals/langfuse/release-review" in paths
    assert "/internal/evals/langfuse/remote-experiment" not in paths
