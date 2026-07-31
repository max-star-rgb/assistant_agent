"""Launch the repository Agent eval CLI from a signed Langfuse webhook."""

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
from typing import Any, Literal, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from assistant_agent.evaluation.remote_run_control import RemoteProgressTracker


REMOTE_EXPERIMENT_ENABLED_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_ENABLED"
)
REMOTE_EXPERIMENT_SIGNING_SECRET_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_SIGNING_SECRET"
)
REMOTE_EXPERIMENT_DATASET_ENV = (
    "ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_DATASET"
)
DEFAULT_REMOTE_EXPERIMENT_DATASET = "assistant-agent-regression"
REMOTE_EXPERIMENT_SIGNATURE_MAX_AGE_SECONDS = 300
_SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$"


class RemoteExperimentPayload(BaseModel):
    """Operator-editable JSON encoded in Langfuse's top-level payload string."""

    model_config = ConfigDict(extra="forbid")

    task: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=_SAFE_IDENTIFIER_PATTERN,
    )
    suite: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=_SAFE_IDENTIFIER_PATTERN,
    )
    run_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=_SAFE_IDENTIFIER_PATTERN,
        validation_alias=AliasChoices("runName", "run_name"),
    )

    @model_validator(mode="after")
    def allow_at_most_one_selector(self) -> RemoteExperimentPayload:
        if self.task is not None and self.suite is not None:
            raise ValueError(
                "At most one of payload.task or payload.suite is allowed."
            )
        return self


class RemoteExperimentRequest(BaseModel):
    """Langfuse Remote Experiment trigger envelope used by this runner."""

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
    payload: str = Field(min_length=2, max_length=4096)


class RemoteExperimentAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    trigger_id: str
    duplicate: bool = False
    dataset_name: str
    selector_kind: Literal["dataset", "task", "suite"]
    selector_id: str
    run_name: str


class RemoteExperimentSettings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = False
    signing_secret: str | None = None
    dataset_name: str = DEFAULT_REMOTE_EXPERIMENT_DATASET
    repository_root: Path
    artifact_root: Path
    signature_max_age_seconds: int = REMOTE_EXPERIMENT_SIGNATURE_MAX_AGE_SECONDS

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        repository_root: Path | None = None,
    ) -> RemoteExperimentSettings:
        root = repository_root or Path(__file__).resolve().parents[3]
        return cls(
            enabled=_truthy(env.get(REMOTE_EXPERIMENT_ENABLED_ENV)),
            signing_secret=_nonempty(
                env.get(REMOTE_EXPERIMENT_SIGNING_SECRET_ENV)
            ),
            dataset_name=(
                _nonempty(env.get(REMOTE_EXPERIMENT_DATASET_ENV))
                or DEFAULT_REMOTE_EXPERIMENT_DATASET
            ),
            repository_root=root,
            artifact_root=root / ".data" / "evals" / "remote",
        )


class RemoteExperimentError(RuntimeError):
    """Base error for safe HTTP mapping."""


class RemoteExperimentDisabled(RemoteExperimentError):
    pass


class RemoteExperimentUnauthorized(RemoteExperimentError):
    pass


class RemoteExperimentInvalid(RemoteExperimentError):
    pass


class RemoteExperimentLaunchFailed(RemoteExperimentError):
    pass


PopenFactory = Callable[..., subprocess.Popen[bytes]]
Reaper = Callable[
    [subprocess.Popen[bytes], Path, dict[str, Any], Path, Path],
    None,
]
ProgressSink = Callable[[str], None]


class ProgressBar(Protocol):
    def update(self, amount: int) -> object: ...

    def close(self) -> object: ...


ProgressFactory = Callable[[int, str], ProgressBar | None]


class RemoteExperimentLauncher:
    """Validate one signed request and start the fixed eval CLI asynchronously."""

    def __init__(
        self,
        settings: RemoteExperimentSettings,
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
    ) -> RemoteExperimentAccepted:
        self._require_ready()
        if not _verify_signature(
            raw_body=raw_body,
            signature_header=signature_header,
            secret=self.settings.signing_secret or "",
            now=self._now(),
            max_age_seconds=self.settings.signature_max_age_seconds,
        ):
            raise RemoteExperimentUnauthorized(
                "Langfuse remote experiment signature is invalid or expired."
            )
        try:
            request = RemoteExperimentRequest.model_validate_json(raw_body)
        except ValidationError as exc:
            raise RemoteExperimentInvalid(
                "Langfuse remote experiment payload is invalid."
            ) from exc
        if request.dataset_name != self.settings.dataset_name:
            raise RemoteExperimentInvalid(
                f"Dataset {request.dataset_name!r} is not allowed."
            )

        try:
            payload = RemoteExperimentPayload.model_validate_json(
                request.payload
            )
        except ValidationError as exc:
            raise RemoteExperimentInvalid(
                "Langfuse remote experiment payload is invalid."
            ) from exc

        selector_kind, selector_id = self._validate_selector(
            payload,
            dataset_name=request.dataset_name,
        )
        trigger_id = _trigger_id(signature_header or "", raw_body)
        run_name = payload.run_name or (
            f"langfuse-ui-{selector_id}-{trigger_id[:8]}"
        )
        accepted = RemoteExperimentAccepted(
            trigger_id=trigger_id,
            dataset_name=request.dataset_name,
            selector_kind=selector_kind,
            selector_id=selector_id,
            run_name=run_name,
        )
        return self._launch_once(accepted)

    def _require_ready(self) -> None:
        if not self.settings.enabled:
            raise RemoteExperimentDisabled(
                "Langfuse remote experiments are disabled."
            )
        if not self.settings.signing_secret:
            raise RemoteExperimentDisabled(
                "Langfuse remote experiment signing secret is not configured."
            )
        if self._env.get("MULTIMODAL_AGENT_PROVIDER_MODE") != "real":
            raise RemoteExperimentDisabled(
                "Langfuse remote experiments require real Provider mode."
            )

    def _validate_selector(
        self,
        payload: RemoteExperimentPayload,
        *,
        dataset_name: str,
    ) -> tuple[Literal["dataset", "task", "suite"], str]:
        if payload.task is not None:
            cases_root = (
                self.settings.repository_root
                / "evals"
                / "agent"
            )
            task_paths = (
                cases_root / level / payload.task / "task.json"
                for level in ("tasks", "missions")
            )
            if not any(path.is_file() for path in task_paths):
                raise RemoteExperimentInvalid(
                    f"Unknown Agent eval task: {payload.task}."
                )
            return "task", payload.task
        if payload.suite is None:
            return "dataset", dataset_name

        suites_path = (
            self.settings.repository_root
            / "evals"
            / "agent"
            / "suites.json"
        )
        try:
            suites = json.loads(suites_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RemoteExperimentDisabled(
                "Agent eval suite registry is unavailable."
            ) from exc
        if payload.suite not in suites:
            raise RemoteExperimentInvalid(
                f"Unknown Agent eval suite: {payload.suite}."
            )
        return "suite", str(payload.suite)

    def _launch_once(
        self,
        accepted: RemoteExperimentAccepted,
    ) -> RemoteExperimentAccepted:
        command = self._command(accepted)
        self.settings.artifact_root.mkdir(parents=True, exist_ok=True)
        receipt_path = (
            self.settings.artifact_root / f"{accepted.trigger_id}.json"
        )
        with self._lock:
            try:
                receipt_fd = os.open(
                    receipt_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                return accepted.model_copy(update={"duplicate": True})

            stdout_path = (
                self.settings.artifact_root
                / f"{accepted.trigger_id}.stdout.log"
            )
            stderr_path = (
                self.settings.artifact_root
                / f"{accepted.trigger_id}.stderr.log"
            )
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
                with os.fdopen(receipt_fd, "w", encoding="utf-8") as receipt_file:
                    json.dump(receipt, receipt_file, ensure_ascii=False)
                    receipt_file.write("\n")
                receipt_fd = -1
                self._reaper(
                    process,
                    receipt_path,
                    receipt,
                    stdout_path,
                    stderr_path,
                )
                return accepted
            except RemoteExperimentLaunchFailed:
                if receipt_fd >= 0:
                    os.close(receipt_fd)
                receipt_path.unlink(missing_ok=True)
                raise
            except (OSError, ValueError) as exc:
                if receipt_fd >= 0:
                    os.close(receipt_fd)
                receipt_path.unlink(missing_ok=True)
                raise RemoteExperimentLaunchFailed(
                    "Failed to start the Agent eval CLI."
                ) from exc
            finally:
                if stdout_file is not None:
                    stdout_file.close()

    def _command(self, accepted: RemoteExperimentAccepted) -> list[str]:
        script_path = (
            self.settings.repository_root / "scripts" / "run_agent_evals.py"
        )
        if not script_path.is_file():
            raise RemoteExperimentLaunchFailed(
                "Agent eval CLI entrypoint is unavailable."
            )
        selector_args = (
            ["--dataset-active"]
            if accepted.selector_kind == "dataset"
            else [
                f"--{accepted.selector_kind}",
                accepted.selector_id,
            ]
        )
        return [
            sys.executable,
            str(script_path),
            "--run",
            *selector_args,
            "--dataset-name",
            accepted.dataset_name,
            "--allow-real-provider",
            "--run-name",
            accepted.run_name,
        ]


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
    parts: dict[str, str] = {}
    for item in signature_header.split(","):
        key, separator, value = item.strip().partition("=")
        if separator and key and value:
            parts[key] = value
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
    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(received, expected)


def _trigger_id(signature_header: str, raw_body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(signature_header.encode("utf-8"))
    digest.update(b"\0")
    digest.update(raw_body)
    return digest.hexdigest()[:24]


def _start_process_reaper(
    process: subprocess.Popen[bytes],
    receipt_path: Path,
    receipt: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    *,
    progress_sink: ProgressSink | None = None,
    progress_factory: ProgressFactory | None = None,
) -> None:
    def reap() -> None:
        sink = progress_sink or _default_progress_sink
        create_progress_bar = progress_factory or _default_progress_factory
        tracker = RemoteProgressTracker()
        progress_bar: ProgressBar | None = None
        stderr_stream = getattr(process, "stderr", None)
        stderr_file = None
        try:
            stderr_file = stderr_path.open("ab")
        except OSError as exc:
            sink(
                f"[agent-eval {str(receipt.get('trigger_id') or '')[:8]}] "
                f"progress-log-error error={exc}"
            )
        try:
            if stderr_stream is not None:
                for raw_line in stderr_stream:
                    if stderr_file is not None:
                        try:
                            stderr_file.write(raw_line)
                            stderr_file.flush()
                        except OSError as exc:
                            stderr_file.close()
                            stderr_file = None
                            sink(
                                f"[agent-eval "
                                f"{str(receipt.get('trigger_id') or '')[:8]}] "
                                f"progress-log-error error={exc}"
                            )
                    completed_before = len(tracker.completed_task_ids)
                    if not _consume_child_progress(raw_line, tracker=tracker):
                        continue
                    if progress_bar is None and tracker.task_count is not None:
                        try:
                            progress_bar = create_progress_bar(
                                tracker.task_count,
                                (
                                    f"[agent-eval "
                                    f"{str(receipt.get('trigger_id') or '')[:8]}]"
                                ),
                            )
                        except Exception:
                            progress_bar = None
                    completed_delta = (
                        len(tracker.completed_task_ids) - completed_before
                    )
                    if progress_bar is not None and completed_delta > 0:
                        try:
                            progress_bar.update(completed_delta)
                        except Exception:
                            progress_bar = None
        finally:
            if stderr_file is not None:
                stderr_file.close()
            if stderr_stream is not None:
                stderr_stream.close()
            if progress_bar is not None:
                try:
                    progress_bar.close()
                except Exception:
                    pass
        exit_code = process.wait()
        latest_receipt = {
            **receipt,
            **(_read_receipt(receipt_path) or {}),
        }
        was_stopped = latest_receipt.get("status") == "stop_requested"
        completed = {
            **latest_receipt,
            "status": (
                "stopped"
                if was_stopped
                else ("completed" if exit_code == 0 else "failed")
            ),
            "exit_code": exit_code,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        temporary_path = receipt_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(completed, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, receipt_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)

    threading.Thread(
        target=reap,
        name=f"agent-eval-reaper-{process.pid}",
        daemon=True,
    ).start()


def _consume_child_progress(
    raw_line: bytes,
    *,
    tracker: RemoteProgressTracker,
) -> bool:
    try:
        payload = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return tracker.consume(payload) is not None


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _default_progress_factory(total: int, description: str) -> ProgressBar | None:
    try:
        from tqdm import tqdm
    except ImportError:
        return None
    return tqdm(
        total=total,
        desc=description,
        unit="task",
        dynamic_ncols=True,
        file=sys.stderr,
    )


def _default_progress_sink(line: str) -> None:
    try:
        from tqdm import tqdm
    except ImportError:
        print(line, file=sys.stderr, flush=True)
        return
    tqdm.write(line, file=sys.stderr)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _nonempty(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None
