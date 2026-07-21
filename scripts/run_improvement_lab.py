#!/usr/bin/env python3
"""Run the offline, non-mutating Improvement Lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.config import ProviderConfig
from assistant_agent.services.chat_adapter import create_chat_adapter
from assistant_agent.services.improvement.lab import run_improvement_lab
from assistant_agent.services.improvement.report import render_improvement_report
from assistant_agent.services.trace_store import JsonlTraceStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate evidence-backed improvement proposals without applying changes."
    )
    parser.add_argument("--trace-path", default=".data/graph_trace.jsonl")
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--trace-id", action="append", default=[])
    parser.add_argument("--eval-report", action="append", type=Path, default=[])
    parser.add_argument("--test-report", action="append", type=Path, default=[])
    parser.add_argument("--target", choices=("all", "skill", "runtime", "code"), default="all")
    parser.add_argument("--skill-id")
    parser.add_argument(
        "--proposal-mode",
        choices=("deterministic", "provider"),
        default="deterministic",
    )
    parser.add_argument("--registry-root", type=Path, default=Path(".data/improvement_lab"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".data/improvement_lab/reports"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--run-allowlisted-evals",
        action="store_true",
        help="Run only repository-owned local test suite commands selected by evaluated candidates.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ProviderConfig.from_env()
        adapter = None
        if args.proposal_mode == "provider" and config.provider_mode == "real":
            adapter = create_chat_adapter(config)
        report = run_improvement_lab(
            trace_store=JsonlTraceStore(args.trace_path),
            run_ids=args.run_id,
            trace_ids=args.trace_id,
            eval_paths=args.eval_report,
            test_paths=args.test_report,
            target_type=None if args.target == "all" else args.target,
            skill_id=args.skill_id,
            repo_root=REPO_ROOT,
            registry_root=args.registry_root,
            persist=not args.dry_run,
            proposal_mode=args.proposal_mode,
            adapter=adapter,
            provider_mode=config.provider_mode,
            run_allowlisted_evals=args.run_allowlisted_evals,
        )
        args.output.mkdir(parents=True, exist_ok=True)
        report_path = args.output / f"{report.run_id}.md"
        report_path.write_text(render_improvement_report(report), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "completed",
                "run_id": report.run_id,
                "evidence_count": len(report.evidence),
                "opportunity_count": len(report.opportunities),
                "candidate_count": len(report.candidates),
                "issue_count": len(report.issues),
                "persisted": report.persisted,
                "report_path": str(report_path),
                "production_mutation_allowed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
