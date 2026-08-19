#!/usr/bin/env python3
"""Run the operator-gated realtime visual target-window system eval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for candidate in (ROOT, SRC):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from evals.system.realtime_visual_target_window.runner import (
    DEFAULT_OUTPUT_ROOT,
    RealtimeVisualEvalConfigurationError,
    dry_run_report,
    run_real_eval,
)  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate one strict five-frame realtime visual target window."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--frame-dir", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()

    if args.dry_run:
        print(
            json.dumps(
                dry_run_report(
                    frame_dir=args.frame_dir,
                    allow_real_provider=args.allow_real_provider,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if args.frame_dir is None:
        print(json.dumps({"status": "blocked", "error": "frame_dir_required"}))
        return 2
    try:
        run_dir, result = run_real_eval(
            frame_dir=args.frame_dir,
            allow_real_provider=args.allow_real_provider,
            output_root=args.output_root,
        )
    except RealtimeVisualEvalConfigurationError as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": "configuration_error", "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "realtime_visual_target_window_eval_failed",
                    "message": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({**result, "run_dir": str(run_dir)}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
