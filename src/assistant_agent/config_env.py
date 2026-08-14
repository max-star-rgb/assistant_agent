"""Small dotenv-style loader shared by operator scripts."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path | str, *, override: bool = False) -> dict[str, str]:
    """Load simple KEY=VALUE lines without adding a dotenv dependency."""

    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        loaded[key] = _strip_env_value(value.strip())
        if override or key not in os.environ:
            os.environ[key] = loaded[key]
    return loaded


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


__all__ = ["load_env_file"]
