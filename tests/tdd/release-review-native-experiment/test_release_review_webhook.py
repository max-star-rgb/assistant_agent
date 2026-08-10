from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

from assistant_agent.evaluation.release_review import (
    RELEASE_REVIEW_DATASET,
    ReleaseReviewInvalid,
    ReleaseReviewLauncher,
    ReleaseReviewSettings,
    ReleaseReviewUnauthorized,
)
from assistant_agent.api.routes_eval_experiments import router


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


def _launcher(tmp_path: Path, commands: list[list[str]]) -> ReleaseReviewLauncher:
    def popen(command, **kwargs):
        commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        return FakeProcess()

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
        now=lambda: 1000,
        reaper=lambda *args: None,
    )


def test_signed_webhook_launches_only_fixed_release_review_argv(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    launcher = _launcher(tmp_path, commands)
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
