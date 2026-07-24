#!/usr/bin/env python3
"""Capture one real Provider context request into a local system-eval artifact."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.config import ProviderConfig
from assistant_agent.services.assistant_run_service import load_env_file
from evals.system.common.preflight import SystemEvalConfigurationError
from evals.system.context.runner import (
    DEFAULT_OUTPUT_ROOT,
    run_context_system_eval,
)


def main() -> int:
    parser = ArgumentParser(description="Run one real context system eval.")
    parser.add_argument(
        "--text",
        default="请简短说明你收到了这条 system context eval 请求，不要调用工具。",
    )
    parser.add_argument("--case-id", default="single_turn_context")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument(
        "--allow-unredacted-context",
        action="store_true",
        help="Required because compiled and Provider requests are written unredacted.",
    )
    args = parser.parse_args()

    if not args.allow_unredacted_context:
        print(
            json.dumps(
                {
                    "error": "unredacted_context_not_authorized",
                    "message": "Rerun with --allow-unredacted-context using synthetic input only.",
                },
                ensure_ascii=False,
            )
        )
        return 2
    if not args.no_env_file:
        load_env_file(args.env_file)
    try:
        run_dir, result = run_context_system_eval(
            text=args.text,
            case_id=args.case_id,
            config=ProviderConfig.from_env(),
            output_root=args.output_root,
        )
    except SystemEvalConfigurationError as exc:
        print(
            json.dumps(
                {"error": "system_context_eval_not_configured", "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    payload = {**result, "run_dir": str(run_dir)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
