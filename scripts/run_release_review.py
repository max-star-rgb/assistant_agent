#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = (PROJECT_ROOT / "src").resolve()
for checkout_path in (str(PROJECT_SRC), str(PROJECT_ROOT)):
    while checkout_path in sys.path:
        sys.path.remove(checkout_path)
sys.path[:0] = [str(PROJECT_SRC), str(PROJECT_ROOT)]

import assistant_agent  # noqa: E402


PACKAGE_FILE = Path(assistant_agent.__file__).resolve()
if not PACKAGE_FILE.is_relative_to(PROJECT_SRC):
    raise RuntimeError(
        "stable eval script must import assistant_agent from the current checkout: "
        f"expected under {PROJECT_SRC}, got {PACKAGE_FILE}"
    )

from evals.release_review.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
