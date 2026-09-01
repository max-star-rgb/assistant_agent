#!/usr/bin/env python3
"""Run the explicitly authorized local SigLIP2 image/text system eval."""

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

from assistant_agent.config import load_app_config  # noqa: E402
from evals.system.multimodal_embedding.runner import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT,
    dry_run_report,
    run_local_model_eval,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate joint local SigLIP2 embeddings."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-local-model", action="store_true")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--cuda-device-id", type=int, default=0)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    model_dir = args.model_dir
    if model_dir is None:
        configured = load_app_config().vision.siglip2_model_dir
        model_dir = Path(configured) if configured else None
    if args.dry_run:
        print(json.dumps(dry_run_report(model_dir), ensure_ascii=False))
        return 0
    if not args.allow_local_model:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "local_model_not_authorized",
                    "message": "Rerun with --allow-local-model.",
                },
                ensure_ascii=False,
            )
        )
        return 2
    if model_dir is None:
        print(json.dumps({"status": "blocked", "error": "model_dir_not_configured"}))
        return 2
    try:
        run_dir, result = run_local_model_eval(
            model_dir=model_dir,
            cuda_device_id=args.cuda_device_id,
            output_root=args.output_root,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "multimodal_embedding_eval_failed",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({**result, "run_dir": str(run_dir)}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
