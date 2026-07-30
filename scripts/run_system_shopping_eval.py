"""Run one operator-authorized direct real-shopping system eval."""

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

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.assistant_run_service import load_env_file
from evals.system.tools.runner import (
    DEFAULT_OUTPUT_ROOT,
    EvalConfigurationError,
    run_direct_shopping_system_eval,
)


DEFAULT_SHOPPING_OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT.parent / "shopping"


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        description=(
            "Call the real shopping_search Tool through ActionValidator and "
            "ToolExecutor, then assert real Haodanku products and links."
        )
    )
    parser.add_argument("--keyword", default="纸巾")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_SHOPPING_OUTPUT_ROOT,
    )
    parser.add_argument(
        "--allow-real-tools",
        action="store_true",
        help="Required operator confirmation before the real shopping call.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)

    if not args.no_env_file:
        load_env_file(args.env_file)
    try:
        result = run_direct_shopping_system_eval(
            config=ProviderConfig.from_env(),
            allow_real_tools=args.allow_real_tools,
            keyword=args.keyword,
            output_root=args.output_root,
        )
    except (EvalConfigurationError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": "direct_shopping_eval_not_authorized",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "schema_version": result.schema_version,
                "passed": result.passed,
                "checks": result.checks,
                "failures": result.failures,
                "provider": result.provider,
                "outcome": result.outcome,
                "selection_count": result.selection_count,
                "product_sources": result.product_sources,
                "product_links": result.product_links,
                "run_id": result.run_id,
                "run_dir": str(result.artifact.run_dir),
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
