#!/usr/bin/env python3
"""Stable entrypoint for the native Durable Workflow LangSmith experiment."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evals.langsmith_workflow_regression.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
