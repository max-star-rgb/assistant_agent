#!/usr/bin/env python3
"""Summarize local assistant observability metrics from redacted trace JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.services.trace_metrics import build_trace_metrics, filter_trace_events, load_trace_events


DEFAULT_TRACE_PATH = ".data/graph_trace.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize redacted assistant trace metrics.")
    parser.add_argument(
        "--trace-path",
        default=DEFAULT_TRACE_PATH,
        help=f"JSONL trace store path. Defaults to {DEFAULT_TRACE_PATH}.",
    )
    parser.add_argument("--user-id", help="Optional user_id filter.")
    parser.add_argument("--session-id", help="Optional session_id filter.")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print a JSON summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trace_path = Path(args.trace_path)
    if not trace_path.exists():
        print(f"trace file not found: {trace_path}", file=sys.stderr)
        return 1

    events = load_trace_events(trace_path)
    events = filter_trace_events(events, user_id=args.user_id, session_id=args.session_id)
    metrics = build_trace_metrics(events)
    metrics["trace_path"] = str(trace_path)
    if args.user_id:
        metrics["user_id"] = args.user_id
    if args.session_id:
        metrics["session_id"] = args.session_id

    if args.json_output:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(format_human(metrics))
    return 0


def format_human(metrics: dict[str, Any]) -> str:
    run = metrics["run"]
    llm = metrics["llm"]
    context = metrics["context"]
    gateway = metrics["gateway"]
    memory = metrics["memory"]
    lines = [
        f"trace metrics path={metrics.get('trace_path', '')}",
        (
            f"runs={run['count']} completed={run['completed']} failed={run['failed']} "
            f"cancelled={run['cancelled']} unknown={run['unknown']} events={metrics['event_count']} "
            f"traces={metrics['trace_count']}"
        ),
        (
            f"success_rate={_percent(run['success_rate'])} failure_rate={_percent(run['failure_rate'])} "
            f"cancel_rate={_percent(run['cancel_rate'])} duration_p50={run['duration_ms']['p50']}ms "
            f"duration_p95={run['duration_ms']['p95']}ms"
        ),
        f"errors total={metrics['errors']['count']} by_code={_format_counts(metrics['errors']['by_code'])}",
        (
            f"llm calls={llm['call_count']} errors={llm['error_count']} "
            f"latency_avg={llm['latency_ms']['avg']}ms tokens={llm['total_tokens']} "
            f"providers={_format_counts(llm['provider_counts'])}"
        ),
        "tools",
    ]
    tools = metrics["tools"]["by_tool"]
    if tools:
        for tool_name, tool in tools.items():
            lines.append(
                (
                    f"- {tool_name} calls={tool['call_count']} failed={tool['failure_count']} "
                    f"failure_rate={_percent(tool['failure_rate'])} latency_avg={tool['latency_ms']['avg']}ms "
                    f"retries={tool['retry_count']} confirmations={tool['confirmation_required_count']}"
                )
            )
    else:
        lines.append("- (none)")
    lines.extend(
        [
            (
                f"context samples={context['sample_count']} avg_budget={_percent(context['average_budget_ratio'])} "
                f"max_budget={_percent(context['max_budget_ratio'])} compactions={context['compaction_triggered_count']} "
                f"overflow_retries={context['overflow_retry_count']} tokens={context['total_tokens']}"
            ),
            (
                f"gateway cancels={gateway['cancel_count']} sources={_format_counts(gateway['cancel_sources'])} "
                f"interrupts={gateway['interrupt_count']} deadline_expired={gateway['deadline_expired_count']}"
            ),
            (
                f"memory retrievals={memory['retrieval_count']} saves={memory['save_count']} "
                f"candidates={memory['save_candidate_count']} saved={memory['saved_count']} "
                f"rejected={memory['rejected_count']}"
            ),
        ]
    )
    return "\n".join(lines)


def _percent(value: int | float) -> str:
    return f"{float(value) * 100:.1f}%"


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
