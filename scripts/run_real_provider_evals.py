"""Run opt-in real chat provider evals for end-to-end agent behavior."""

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
from evals.real_provider import (
    DEFAULT_CASES_PATH,
    DEFAULT_OUTPUT_ROOT,
    EvalConfigurationError,
    filter_real_provider_eval_cases,
    load_real_provider_eval_cases,
    run_real_provider_eval_suite,
    validate_real_provider_config,
)


def main() -> int:
    parser = ArgumentParser(description="Run opt-in real chat provider eval cases.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--suite", default="personal_assistant_daily")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--allow-real-tools", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Validate and list selected cases without provider calls.")
    parser.add_argument("--no-fail-on-regression", action="store_true")
    args = parser.parse_args()

    cases = filter_real_provider_eval_cases(
        load_real_provider_eval_cases(args.cases),
        suite=args.suite,
        case_ids=set(args.case_id),
        max_cases=args.max_cases,
    )
    if not cases:
        print(json.dumps({"error": "no_cases_selected", "cases_path": str(args.cases)}, ensure_ascii=False))
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "cases_path": str(args.cases),
                    "selected_case_count": len(cases),
                    "case_ids": [case.id for case in cases],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    config = ProviderConfig.from_env()
    try:
        validate_real_provider_config(config)
    except EvalConfigurationError as exc:
        print(json.dumps({"error": "real_provider_eval_not_configured", "message": str(exc)}, ensure_ascii=False))
        return 2

    run = run_real_provider_eval_suite(
        cases,
        config=config,
        output_root=args.output_root,
        suite_name=args.suite,
        allow_real_tools=args.allow_real_tools,
    )
    payload = {
        **run.summary,
        "run_dir": str(run.artifact.run_dir),
        "summary_path": str(run.artifact.summary_path),
        "results_path": str(run.artifact.results_path),
        "trace_path": str(run.artifact.trace_path),
        "cases_path": str(run.artifact.cases_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if run.summary["failed"] and not args.no_fail_on_regression:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
