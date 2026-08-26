"""Stable entry point for the native AI coding behavior system evaluation."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from evals.system.ai_coding_behavior.runner import main


if __name__ == "__main__":
    raise SystemExit(main())
