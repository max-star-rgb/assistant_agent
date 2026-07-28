#!/usr/bin/env python3
"""Run the task-centered Agent evaluation framework."""

# ruff: noqa: E402

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evals.agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
