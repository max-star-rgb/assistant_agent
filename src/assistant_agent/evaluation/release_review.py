"""Launch the fixed Release Review CLI from a signed Langfuse webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from assistant_agent.evaluation.remote_run_control import RemoteProgressTracker


RELEASE_REVIEW_DATASET = "assistant-agent-release-review"
RELEASE_REVIEW_ENABLED_ENV = "ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_ENABLED"
RELEASE_REVIEW_SIGNING_SECRET_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_SIGNING_SECRET"
)
RELEASE_REVIEW_STAGING_READY_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_STAGING_READY"
)
SIGNATURE_MAX_AGE_SECONDS = 300
_SAFE_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class ReleaseReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(
        pattern=_SAFE_IDENTIFIER,
        validation_alias=AliasChoices("releaseId", "release_id"),
    )
    scenarios: tuple[str, ...] | None = Field(default=None, max_length=32)
    run_name: str | None = Field(
        default=None,
        pattern=_SAFE_IDENTIFIER,
        validation_alias=AliasChoices("runName", "run_name"),
    )


class LangfuseReleaseReviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("projectId", "project_id"),
    )
    dataset_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("datasetId", "dataset_id"),
    )
    dataset_name: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("datasetName", "dataset_name"),
    )
    payload: str = Field(min_length=2, max_length=8_192)


class ReleaseReviewAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    trigger_id: str
    duplicate: bool = False
    dataset_name: Literal["assistant-agent-release-review"] = RELEASE_REVIEW_DATASET
    release_id: str
    scenario_ids: tuple[str, ...] | None = None
    run_name: str


class ReleaseReviewSettings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = False
    signing_secret: str | None = None
    staging_ready: bool = False
    repository_root: Path
    artifact_root: Path
    signature_max_age_seconds: int = SIGNATURE_MAX_AGE_SECONDS

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        repository_root: Path | None = None,
    ) -> "ReleaseReviewSettings":
        root = repository_root or Path(__file__).resolve().parents[3]
        return cls(
            enabled=_truthy(env.get(RELEASE_REVIEW_ENABLED_ENV)),
            signing_secret=_nonempty(env.get(RELEASE_REVIEW_SIGNING_SECRET_ENV)),
            staging_ready=_truthy(env.get(RELEASE_REVIEW_STAGING_READY_ENV)),
            repository_root=root,
            artifact_root=root / ".data" / "evals" / "release_review" / "remote",
        )


class ReleaseReviewError(RuntimeError):
    pass


class ReleaseReviewDisabled(ReleaseReviewError):
    pass


class ReleaseReviewUnauthorized(ReleaseReviewError):
    pass


class ReleaseReviewInvalid(ReleaseReviewError):
    pass


class ReleaseReviewLaunchFailed(ReleaseReviewError):
    pass


PopenFactory = Callable[..., subprocess.Popen[bytes]]
Reaper = Callable[
    [subprocess.Popen[bytes], Path, dict[str, Any], Path, Path], None
]


class ReleaseReviewLauncher:
    def __init__(
        self,
        settings: ReleaseReviewSettings,
        *,
        env: Mapping[str, str] | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        now: Callable[[], float] = time.time,
        reaper: Reaper | None = None,
    ) -> None:
        self.settings = settings
        self._env = dict(env if env is not None else os.environ)
        self._popen_factory = popen_factory
        self._now = now
        self._reaper = reaper or _start_process_reaper
        self._lock = threading.Lock()

    def launch(
        self,
        *,
        raw_body: bytes,
        signature_header: str | None,
    ) -> ReleaseReviewAccepted:
        self._require_ready()
        if not _verify_signature(
            raw_body=raw_body,
            signature_header=signature_header,
            secret=self.settings.signing_secret or "",
            now=self._now(),
            max_age_seconds=self.settings.signature_max_age_seconds,
        ):
            raise ReleaseReviewUnauthorized(
                "Langfuse Release Review signature is invalid or expired."
            )
        try:
            request = LangfuseReleaseReviewRequest.model_validate_json(raw_body)
        except ValidationError as exc:
            raise ReleaseReviewInvalid("Langfuse Release Review request is invalid.") from exc
        if request.dataset_name != RELEASE_REVIEW_DATASET:
            raise ReleaseReviewInvalid(f"Dataset {request.dataset_name!r} is not allowed.")
        try:
            payload = ReleaseReviewPayload.model_validate_json(request.payload)
        except ValidationError as exc:
            raise ReleaseReviewInvalid("Langfuse Release Review payload is invalid.") from exc
        scenario_ids = self._validate_scenarios(payload.scenarios)
        trigger_id = _trigger_id(signature_header or "", raw_body)
        run_name = payload.run_name or (
            f"release-review-{payload.release_id}-{trigger_id[:8]}"
        )
        accepted = ReleaseReviewAccepted(
            trigger_id=trigger_id,
            release_id=payload.release_id,
            scenario_ids=scenario_ids,
            run_name=run_name,
        )
        return self._launch_once(accepted, payload)

    def _require_ready(self) -> None:
        if not self.settings.enabled:
            raise ReleaseReviewDisabled("Langfuse Release Review is disabled.")
        if not self.settings.signing_secret:
            raise ReleaseReviewDisabled("Release Review signing secret is not configured.")
        if not self.settings.staging_ready:
            raise ReleaseReviewDisabled("Release Review staging resources are not ready.")
        if self._env.get("MULTIMODAL_AGENT_PROVIDER_MODE") != "real":
            raise ReleaseReviewDisabled("Release Review requires real Provider mode.")

    def _validate_scenarios(
        self, scenario_ids: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if scenario_ids is None:
            return None
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ReleaseReviewInvalid("Release Review scenario ids must be unique.")
        root = self.settings.repository_root / "evals" / "release_review" / "scenarios"
        for scenario_id in scenario_ids:
            if not re_fullmatch_safe(scenario_id) or not (root / f"{scenario_id}.yaml").is_file():
                raise ReleaseReviewInvalid(f"Unknown Release Review scenario: {scenario_id}.")
        return scenario_ids

    def _launch_once(
        self,
        accepted: ReleaseReviewAccepted,
        payload: ReleaseReviewPayload,
    ) -> ReleaseReviewAccepted:
        command = self._command(accepted, payload)
        self.settings.artifact_root.mkdir(parents=True, exist_ok=True)
        receipt_path = self.settings.artifact_root / f"{accepted.trigger_id}.json"
        with self._lock:
            try:
                receipt_fd = os.open(
                    receipt_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                return accepted.model_copy(update={"duplicate": True})
            stdout_path = self.settings.artifact_root / f"{accepted.trigger_id}.stdout.log"
            stderr_path = self.settings.artifact_root / f"{accepted.trigger_id}.stderr.log"
            stdout_file = None
            try:
                stdout_file = stdout_path.open("ab")
                process = self._popen_factory(
                    command,
                    cwd=self.settings.repository_root,
                    env=self._env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    shell=False,
                    close_fds=True,
                )
                receipt = {
                    **accepted.model_dump(mode="json"),
                    "pid": process.pid,
                    "command": command,
                    "created_at": datetime.now(UTC).isoformat(),
                    "stdout_log": str(stdout_path),
                    "stderr_log": str(stderr_path),
                }
                with os.fdopen(receipt_fd, "w", encoding="utf-8") as stream:
                    json.dump(receipt, stream, ensure_ascii=False)
                    stream.write("\n")
                receipt_fd = -1
                self._reaper(process, receipt_path, receipt, stdout_path, stderr_path)
                return accepted
            except (OSError, ValueError) as exc:
                if receipt_fd >= 0:
                    os.close(receipt_fd)
                receipt_path.unlink(missing_ok=True)
                raise ReleaseReviewLaunchFailed(
                    "Failed to start the Release Review CLI."
                ) from exc
            finally:
                if stdout_file is not None:
                    stdout_file.close()

    def _command(
        self,
        accepted: ReleaseReviewAccepted,
        payload: ReleaseReviewPayload,
    ) -> list[str]:
        script = self.settings.repository_root / "scripts" / "run_release_review.py"
        if not script.is_file():
            raise ReleaseReviewLaunchFailed("Release Review CLI entrypoint is unavailable.")
        command = [
            sys.executable,
            str(script),
            "--run",
            "--release-id",
            accepted.release_id,
            "--allow-real-provider",
            "--allow-staging-side-effects",
        ]
        for scenario_id in accepted.scenario_ids or ():
            command.extend(("--scenario", scenario_id))
        command.extend(("--run-name", accepted.run_name))
        return command


def _verify_signature(
    *,
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
    now: float,
    max_age_seconds: int,
) -> bool:
    if not signature_header or not secret:
        return False
    parts = dict(
        item.strip().partition("=")[::2]
        for item in signature_header.split(",")
        if "=" in item
    )
    timestamp = parts.get("t")
    signature = parts.get("v1") or parts.get("s")
    if timestamp is None or signature is None:
        return False
    try:
        timestamp_seconds = int(timestamp)
        received = bytes.fromhex(signature)
    except ValueError:
        return False
    if abs(now - timestamp_seconds) > max_age_seconds:
        return False
    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(received, expected)


def _trigger_id(signature_header: str, raw_body: bytes) -> str:
    digest = hashlib.sha256(signature_header.encode() + b"\0" + raw_body)
    return digest.hexdigest()[:24]


def _start_process_reaper(
    process: subprocess.Popen[bytes],
    receipt_path: Path,
    receipt: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    del stdout_path

    def reap() -> None:
        tracker = RemoteProgressTracker()
        stderr_stream = getattr(process, "stderr", None)
        if stderr_stream is not None:
            with stderr_path.open("ab") as stderr_file:
                for line in stderr_stream:
                    stderr_file.write(line)
                    stderr_file.flush()
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if isinstance(payload, dict):
                        tracker.consume(payload)
            stderr_stream.close()
        exit_code = process.wait()
        completed = {
            **receipt,
            "status": "completed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        temporary = receipt_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(completed, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, receipt_path)
        except OSError:
            temporary.unlink(missing_ok=True)

    threading.Thread(
        target=reap,
        name=f"release-review-reaper-{process.pid}",
        daemon=True,
    ).start()


def re_fullmatch_safe(value: str) -> bool:
    import re

    return re.fullmatch(_SAFE_IDENTIFIER, value) is not None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _nonempty(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None
