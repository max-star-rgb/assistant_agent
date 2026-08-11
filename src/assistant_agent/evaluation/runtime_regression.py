"""Launch the fixed Runtime Regression Experiment from a Langfuse webhook."""

from __future__ import annotations

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

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from assistant_agent.evaluation.release_review import (
    SIGNATURE_MAX_AGE_SECONDS,
    _nonempty,
    _preflight_error_message,
    _start_process_reaper,
    _trigger_id,
    _truthy,
    _verify_signature,
)


RUNTIME_REGRESSION_ENABLED_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RUNTIME_REGRESSION_ENABLED"
)
RUNTIME_REGRESSION_SIGNING_SECRET_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RUNTIME_REGRESSION_SIGNING_SECRET"
)
RUNTIME_REGRESSION_READY_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_RUNTIME_REGRESSION_READY"
)
_SAFE_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class RuntimeRegressionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_name: str | None = Field(
        default=None,
        pattern=_SAFE_IDENTIFIER,
        validation_alias=AliasChoices("runName", "run_name"),
    )


class LangfuseRuntimeRegressionRequest(BaseModel):
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


class RuntimeRegressionAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    trigger_id: str
    duplicate: bool = False
    dataset_name: Literal["assistant-agent-runtime-regressions"] = (
        RUNTIME_REGRESSION_DATASET
    )
    run_name: str


class RuntimeRegressionSettings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = False
    signing_secret: str | None = None
    runtime_ready: bool = False
    repository_root: Path
    artifact_root: Path
    signature_max_age_seconds: int = SIGNATURE_MAX_AGE_SECONDS

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        repository_root: Path | None = None,
    ) -> "RuntimeRegressionSettings":
        root = repository_root or Path(__file__).resolve().parents[3]
        return cls(
            enabled=_truthy(env.get(RUNTIME_REGRESSION_ENABLED_ENV)),
            signing_secret=_nonempty(env.get(RUNTIME_REGRESSION_SIGNING_SECRET_ENV)),
            runtime_ready=_truthy(env.get(RUNTIME_REGRESSION_READY_ENV)),
            repository_root=root,
            artifact_root=root / ".data" / "evals" / "runtime_regression" / "remote",
        )


class RuntimeRegressionError(RuntimeError):
    pass


class RuntimeRegressionDisabled(RuntimeRegressionError):
    pass


class RuntimeRegressionUnauthorized(RuntimeRegressionError):
    pass


class RuntimeRegressionInvalid(RuntimeRegressionError):
    pass


class RuntimeRegressionLaunchFailed(RuntimeRegressionError):
    pass


class RuntimeRegressionPreflightFailed(RuntimeRegressionLaunchFailed):
    pass


PopenFactory = Callable[..., subprocess.Popen[bytes]]
RunFactory = Callable[..., subprocess.CompletedProcess[str]]
Reaper = Callable[[subprocess.Popen[bytes], Path, dict[str, Any], Path, Path], None]


class RuntimeRegressionLauncher:
    def __init__(
        self,
        settings: RuntimeRegressionSettings,
        *,
        env: Mapping[str, str] | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        run_factory: RunFactory = subprocess.run,
        now: Callable[[], float] = time.time,
        reaper: Reaper | None = None,
    ) -> None:
        self.settings = settings
        self._env = dict(env if env is not None else os.environ)
        self._popen_factory = popen_factory
        self._run_factory = run_factory
        self._now = now
        self._reaper = reaper or _start_process_reaper
        self._lock = threading.Lock()

    def launch(
        self,
        *,
        raw_body: bytes,
        signature_header: str | None,
    ) -> RuntimeRegressionAccepted:
        self._require_ready()
        if not _verify_signature(
            raw_body=raw_body,
            signature_header=signature_header,
            secret=self.settings.signing_secret or "",
            now=self._now(),
            max_age_seconds=self.settings.signature_max_age_seconds,
        ):
            raise RuntimeRegressionUnauthorized(
                "Langfuse Runtime Regression signature is invalid or expired."
            )
        try:
            request = LangfuseRuntimeRegressionRequest.model_validate_json(raw_body)
        except ValidationError as exc:
            raise RuntimeRegressionInvalid(
                "Langfuse Runtime Regression request is invalid."
            ) from exc
        if request.dataset_name != RUNTIME_REGRESSION_DATASET:
            raise RuntimeRegressionInvalid(
                f"Dataset {request.dataset_name!r} is not allowed; use "
                f"{RUNTIME_REGRESSION_DATASET!r}."
            )
        try:
            payload = RuntimeRegressionPayload.model_validate_json(request.payload)
        except ValidationError as exc:
            raise RuntimeRegressionInvalid(
                "Langfuse Runtime Regression payload is invalid."
            ) from exc
        trigger_id = _trigger_id(signature_header or "", raw_body)
        accepted = RuntimeRegressionAccepted(
            trigger_id=trigger_id,
            run_name=payload.run_name or f"runtime-regression-{trigger_id[:8]}",
        )
        return self._launch_once(accepted)

    def _require_ready(self) -> None:
        if not self.settings.enabled:
            raise RuntimeRegressionDisabled("Langfuse Runtime Regression is disabled.")
        if not self.settings.signing_secret:
            raise RuntimeRegressionDisabled(
                "Runtime Regression signing secret is not configured."
            )
        if not self.settings.runtime_ready:
            raise RuntimeRegressionDisabled(
                "Runtime Regression side effects are not operator-approved."
            )
        if self._env.get("MULTIMODAL_AGENT_PROVIDER_MODE") != "real":
            raise RuntimeRegressionDisabled(
                "Runtime Regression requires real Provider mode."
            )

    def _launch_once(
        self,
        accepted: RuntimeRegressionAccepted,
    ) -> RuntimeRegressionAccepted:
        self._run_preflight(self._preflight_command())
        command = self._command(accepted)
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
                raise RuntimeRegressionLaunchFailed(
                    "Failed to start the Runtime Regression CLI."
                ) from exc
            finally:
                if stdout_file is not None:
                    stdout_file.close()

    def _script(self) -> Path:
        script = self.settings.repository_root / "scripts" / "run_runtime_regressions.py"
        if not script.is_file():
            raise RuntimeRegressionLaunchFailed(
                "Runtime Regression CLI entrypoint is unavailable."
            )
        return script

    def _preflight_command(self) -> list[str]:
        return [
            sys.executable,
            str(self._script()),
            "--preflight",
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]

    def _command(self, accepted: RuntimeRegressionAccepted) -> list[str]:
        return [
            sys.executable,
            str(self._script()),
            "--run",
            "--run-name",
            accepted.run_name,
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]

    def _run_preflight(self, command: list[str]) -> None:
        try:
            completed = self._run_factory(
                command,
                cwd=self.settings.repository_root,
                env=self._env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeRegressionPreflightFailed(
                "Runtime Regression preflight could not complete."
            ) from exc
        if completed.returncode != 0:
            raise RuntimeRegressionPreflightFailed(
                "Runtime Regression preflight failed: "
                + _preflight_error_message(completed.stdout)
            )
