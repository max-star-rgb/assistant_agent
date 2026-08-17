"""Run one isolated local SQLite calendar_create system eval."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evals.system.tools.calendar_create import (
    DEFAULT_OUTPUT_ROOT,
    CalendarCreateEvalInput,
    run_local_calendar_create_eval,
    validate_calendar_create_output_root,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        description=(
            "Execute calendar_create against an isolated real SQLite calendar "
            "through LangGraph ToolNode, then verify idempotency."
        )
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--title",
        default="assistant_agent 本地日历创建评测",
    )
    parser.add_argument(
        "--start-time",
        default="2030-01-15T09:00:00+08:00",
    )
    parser.add_argument(
        "--end-time",
        default="2030-01-15T09:30:00+08:00",
    )
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--location", default="system-eval")
    parser.add_argument("--notes", default="synthetic system eval event")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the synthetic event and output root without creating files.",
    )
    args = parser.parse_args(argv)

    eval_input = CalendarCreateEvalInput(
        title=args.title,
        start_time=args.start_time,
        end_time=args.end_time,
        timezone=args.timezone,
        location=args.location,
        notes=args.notes,
    )
    if args.dry_run:
        try:
            output_root = validate_calendar_create_output_root(args.output_root)
        except ValueError as exc:
            print(
                json.dumps(
                    {
                        "error": "local_calendar_create_eval_invalid_configuration",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "output_root": str(output_root),
                    "input": {
                        **eval_input.model_dump(mode="json"),
                        "title": f"{eval_input.title} <run_id>",
                    },
                    "governance_chain": ["ToolNode", "CalendarCreateTool"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "status": "running",
                "phase": "calendar_create",
                "message": (
                    "正在执行隔离的真实 SQLite 日历写入与幂等回放检查。"
                ),
                "output_root": str(args.output_root),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )

    try:
        result = run_local_calendar_create_eval(
            eval_input=eval_input,
            output_root=args.output_root,
        )
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "error": "local_calendar_create_eval_invalid_configuration",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                **result.model_dump(mode="json", exclude={"artifact"}),
                "run_dir": str(result.artifact.run_dir),
                "database_path": str(result.artifact.database_path),
                "summary_path": str(result.artifact.summary_path),
                "result_path": str(result.artifact.result_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
