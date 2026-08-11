#!/usr/bin/env python3
"""Stable entrypoint for the production-derived LangSmith regression loop."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.langsmith_runtime_regression.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
