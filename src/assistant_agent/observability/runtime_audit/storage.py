"""Filesystem artifacts for resumable runtime audit runs."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

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
            self.watermark_path,
            json.dumps(watermark.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        return path

    def write_deterministic_report(self, bundle: RuntimeAuditBundle, markdown: str) -> Path:
        path = self.reports_dir / f"{bundle.audit_run_id}.md"
        _atomic_write(path, markdown)
        return path

    def codex_json_path(self, audit_run_id: str) -> Path:
        return self.reports_dir / f"{audit_run_id}.json"

    def codex_schema_path(self, audit_run_id: str) -> Path:
        return self.state_dir / f"{audit_run_id}.report-schema.json"

    def _artifact_exists(self, audit_run_id: str) -> bool:
        return any(
            path.exists()
            for path in (
                self.inbox_dir / f"{audit_run_id}.json",
                self.reports_dir / f"{audit_run_id}.json",
                self.reports_dir / f"{audit_run_id}.md",
                self.state_dir / f"{audit_run_id}.report-schema.json",
            )
        )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    temporary.replace(path)


def _iso_z(value) -> str:
    return value.isoformat().replace("+00:00", "Z")
