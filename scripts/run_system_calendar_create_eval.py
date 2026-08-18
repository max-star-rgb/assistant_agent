"""Compatibility PyCharm entry for the fixed-input calendar_create smoke."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


TARGET = Path(__file__).resolve().parents[1] / "evals" / "system" / "tools" / "calendar_create.py"


def main() -> int:
    if str(TARGET.parent) not in sys.path:
        sys.path.insert(0, str(TARGET.parent))
    try:
        runpy.run_path(str(TARGET), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
