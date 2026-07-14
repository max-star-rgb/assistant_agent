"""Score measured Hindsight/Mem0 bake-off metrics without invoking providers."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.memory.framework.bakeoff import (
    FrameworkBakeoffMetrics,
    select_framework_winner,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hindsight-metrics", type=Path, required=True)
    parser.add_argument("--mem0-metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    hindsight = _load(args.hindsight_metrics, expected="hindsight")
    mem0 = _load(args.mem0_metrics, expected="mem0")
    decision = select_framework_winner(hindsight, mem0)
    report = {
        "schema_version": 1,
        "fixed_versions": {"hindsight": "0.8.4", "mem0": "2.0.11"},
        "decision": decision.model_dump(mode="json"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _load(path: Path, *, expected: str) -> FrameworkBakeoffMetrics:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metrics = FrameworkBakeoffMetrics.model_validate(payload).validate_fixed_version()
    if metrics.framework != expected:
        raise ValueError(f"expected {expected} metrics, got {metrics.framework}")
    return metrics


if __name__ == "__main__":
    raise SystemExit(main())
