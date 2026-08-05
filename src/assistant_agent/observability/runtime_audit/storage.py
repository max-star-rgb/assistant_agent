"""Filesystem artifacts for resumable runtime audit runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from assistant_agent.observability.runtime_audit.models import RuntimeAuditBundle


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

    def write_bundle(self, bundle: RuntimeAuditBundle) -> Path:
        path = self.inbox_dir / f"{bundle.audit_run_id}.json"
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


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    temporary.replace(path)


def _iso_z(value) -> str:
    return value.isoformat().replace("+00:00", "Z")
