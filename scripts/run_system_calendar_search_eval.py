"""Run one isolated local SQLite calendar_search system eval."""

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

from evals.system.tools.calendar_search import (
    DEFAULT_OUTPUT_ROOT,
    CalendarSearchEvalInput,
    run_local_calendar_search_eval,
    validate_calendar_search_output_root,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        description=(
            "Execute calendar_search against an isolated seeded real SQLite "
            "calendar through ActionValidator and ToolExecutor."
        )
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--query",
        default="assistant_agent 本地日历搜索评测",
    )
    parser.add_argument(
        "--seed-title",
        default="assistant_agent 本地日历搜索评测",
    )
    parser.add_argument(
        "--start-time",
        default="2030-01-16T10:00:00+08:00",
    )
    parser.add_argument(
        "--end-time",
        default="2030-01-16T10:30:00+08:00",
    )
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--location", default="system-eval")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the search and seed event without creating files.",
    )
    args = parser.parse_args(argv)

    eval_input = CalendarSearchEvalInput(
        query=args.query,
        seed_title=args.seed_title,
        start_time=args.start_time,
        end_time=args.end_time,
        timezone=args.timezone,
        location=args.location,
    )
    if args.dry_run:
        try:
            output_root = validate_calendar_search_output_root(args.output_root)
        except ValueError as exc:
            print(
                json.dumps(
                    {
                        "error": "local_calendar_search_eval_invalid_configuration",
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
                        "seed_title": f"{eval_input.seed_title} <run_id>",
                    },
                    "setup": "seed one synthetic event through the local SQLite adapter",
                    "governance_chain": [
                        "ActionValidator",
                        "ToolExecutor",
                        "ToolRegistry",
                        "CalendarSearchTool",
                    ],
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
                "phase": "calendar_search",
                "message": (
                    "正在预置隔离事件并执行真实 SQLite 日历搜索检查。"
                ),
                "output_root": str(args.output_root),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )

    try:
        result = run_local_calendar_search_eval(
            eval_input=eval_input,
            output_root=args.output_root,
        )
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "error": "local_calendar_search_eval_invalid_configuration",
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
