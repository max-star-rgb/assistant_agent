"""Filesystem artifacts for resumable runtime audit runs."""

from __future__ import annotations

from datetime import date, datetime
from contextlib import contextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from assistant_agent.observability.runtime_audit.daily_models import (
    DailyAuditAttempt,
    DailyAuditWatermarkV2,
    IssueRegistry,
)
from assistant_agent.observability.runtime_audit.models import RuntimeAuditBundle


_AUDIT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_audit_run_id(collected_at: datetime) -> str:
    """Format a readable minute-level audit identifier in the local audit timezone."""

    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    local_time = collected_at.astimezone(_AUDIT_TIMEZONE)
    return f"runtime_audit_{local_time:%Y%m%d_%H%M}"


class RuntimeAuditWatermark(BaseModel):
    schema_version: Literal["assistant_agent_runtime_audit_watermark_v1"] = (
        "assistant_agent_runtime_audit_watermark_v1"
    )
    audit_run_id: str
    last_window_end: str
    bundle_path: str


class RuntimeAuditArtifactStore:
    """Persist audit inputs and reports without touching production state."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.inbox_dir = self.root / "inbox"
        self.reports_dir = self.root / "reports"
        self.attempts_dir = self.state_dir / "attempts"
        self.schemas_dir = self.state_dir / "schemas"
        self.commits_dir = self.state_dir / "commits"
        self.lock_path = self.state_dir / "daily-run.lock"
        self.issues_path = self.state_dir / "issues.json"
        self.latest_bundle_path = self.state_dir / "latest-bundle.json"
        self.watermark_path = self.state_dir / "watermark.json"

    def allocate_audit_run_id(self, collected_at: datetime) -> str:
        """Return the first unused minute-level identifier across all audit artifacts."""

        base = format_audit_run_id(collected_at)
        sequence = 1
        while True:
            audit_run_id = base if sequence == 1 else f"{base}_{sequence:02d}"
            if not self._artifact_exists(audit_run_id):
                return audit_run_id
            sequence += 1

    def write_bundle(self, bundle: RuntimeAuditBundle) -> Path:
        path = self.inbox_dir / f"{bundle.audit_run_id}.json"
        if path.exists():
            raise FileExistsError(f"Runtime audit bundle already exists: {path}")
        _atomic_write(path, bundle.model_dump_json(indent=2))
        watermark = RuntimeAuditWatermark(
            audit_run_id=bundle.audit_run_id,
            last_window_end=_iso_z(bundle.window_end),
            bundle_path=str(path),
        )
        _atomic_write(
            self.latest_bundle_path,
            json.dumps(watermark.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        return path

    def write_deterministic_report(self, bundle: RuntimeAuditBundle, markdown: str) -> Path:
        path = self.attempts_dir / f"{bundle.audit_run_id}.deterministic.md"
        _atomic_write(path, markdown)
        return path

    def codex_json_path(self, audit_run_id: str) -> Path:
        return self.attempts_dir / f"{audit_run_id}.codex.json"

    def codex_schema_path(self, audit_run_id: str) -> Path:
        return self.schemas_dir / f"{audit_run_id}.report-schema.json"

    def write_attempt(self, attempt: DailyAuditAttempt) -> Path:
        path = self.attempts_dir / f"{attempt.attempt_id}.json"
        _atomic_write(path, attempt.model_dump_json(indent=2))
        return path

    def commit_intent_path(self, attempt_id: str) -> Path:
        return self.commits_dir / f"{attempt_id}.json"

    def write_commit_intent(self, attempt: DailyAuditAttempt, *, markdown: str, registry: IssueRegistry | None, commit_continuous_state: bool) -> Path:
        path = self.commit_intent_path(attempt.attempt_id)
        payload = {
            "schema_version": "assistant_agent_daily_commit_intent_v2",
            "attempt": attempt.model_dump(mode="json"),
            "markdown": markdown,
            "registry": registry.model_dump(mode="json") if registry is not None else None,
            "commit_continuous_state": commit_continuous_state,
            "expected_predecessor_watermark": self.last_completed_date().isoformat() if self.last_completed_date() else None,
            "previous_registry_digest": self.issue_registry_digest(),
            "desired_registry_digest": _registry_digest(registry) if registry is not None else None,
        }
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
        return path

    def issue_registry_digest(self) -> str:
        return _registry_digest(self.read_issue_registry())

    def read_commit_intents(self) -> list[dict]:
        if not self.commits_dir.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.commits_dir.glob("*.json"))]

    def clear_commit_intent(self, attempt_id: str) -> None:
        self.commit_intent_path(attempt_id).unlink(missing_ok=True)

    @contextmanager
    def daily_claim(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_issue_registry(self) -> IssueRegistry:
        if not self.issues_path.exists():
            return IssueRegistry()
        return IssueRegistry.model_validate_json(
            self.issues_path.read_text(encoding="utf-8")
        )

    def write_issue_registry(self, registry: IssueRegistry) -> Path:
        _atomic_write(self.issues_path, registry.model_dump_json(indent=2))
        return self.issues_path

    def daily_report_path(self, audit_date: date) -> Path:
        return self.reports_dir / f"{audit_date.isoformat()}.md"

    def write_daily_report(self, audit_date: date, markdown: str, *, replace: bool = True) -> Path:
        path = self.daily_report_path(audit_date)
        if replace:
            _atomic_write(path, markdown)
        else:
            _atomic_write_if_absent(path, markdown)
        return path

    def write_failed_daily_report_if_absent(self, audit_date: date, markdown: str) -> Path:
        return self.write_daily_report(audit_date, markdown, replace=False)

    def mark_day_completed(
        self,
        audit_date: date,
        *,
        attempt_id: str,
        bundle_path: str,
    ) -> Path:
        current = self.read_daily_watermark()
        if current is not None:
            if current.last_completed_date == audit_date and current.last_attempt_id == attempt_id:
                return self.watermark_path
            if audit_date != current.last_completed_date.fromordinal(
                current.last_completed_date.toordinal() + 1
            ):
                raise ValueError("daily watermark must advance exactly one calendar day")
        watermark = DailyAuditWatermarkV2(
            last_completed_date=audit_date,
            last_attempt_id=attempt_id,
            bundle_path=bundle_path,
        )
        _atomic_write(
            self.watermark_path,
            json.dumps(watermark.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        return self.watermark_path

    def last_completed_date(self) -> date | None:
        watermark = self.read_daily_watermark()
        return watermark.last_completed_date if watermark else None

    def read_daily_watermark(self) -> DailyAuditWatermarkV2 | None:
        if not self.watermark_path.exists():
            return None
        payload = json.loads(self.watermark_path.read_text(encoding="utf-8"))
        if (
            payload.get("schema_version")
            != "assistant_agent_runtime_audit_watermark_v2"
        ):
            return None
        return DailyAuditWatermarkV2.model_validate(payload)

    def is_day_completed(self, audit_date: date) -> bool:
        """Whether a successful checkpoint proves this date already has a good report."""

        completed = self.last_completed_date()
        return completed is not None and completed >= audit_date

    def _artifact_exists(self, audit_run_id: str) -> bool:
        return any(
            path.exists()
            for path in (
                self.inbox_dir / f"{audit_run_id}.json",
                self.reports_dir / f"{audit_run_id}.md",
                self.attempts_dir / f"{audit_run_id}.codex.json",
                self.schemas_dir / f"{audit_run_id}.report-schema.json",
                self.attempts_dir / f"{audit_run_id}.json",
            )
        )


def _atomic_write(path: Path, content: str) -> None:
    temporary = _write_temporary(path, content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_if_absent(path: Path, content: str) -> bool:
    """Publish fully written content once without replacing an existing artifact."""

    temporary = _write_temporary(path, content)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _write_temporary(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_with_trailing_newline(content))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _with_trailing_newline(content: str) -> str:
    return content + ("" if content.endswith("\n") else "\n")


def _iso_z(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _registry_digest(registry: IssueRegistry | None) -> str:
    payload = registry.model_dump_json() if registry is not None else ""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
