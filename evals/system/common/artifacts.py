"""Local, machine-readable artifacts for system evals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def create_run_dir(root: Path, *, domain: str, case_id: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / f"{timestamp}_{_safe(domain)}_{_safe(case_id)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _safe(value: str) -> str:
    return "".join(
        character
        if character.isalnum() or character in {"-", "_"}
        else "-"
        for character in value
    )[:80]
