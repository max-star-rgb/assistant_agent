"""Offline environment checks for release validation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


REQUIRED_IMPORTS = (
    "fastapi",
    "langgraph",
    "pydantic",
    "pytest",
    "multimodal_agent.agent.runtime",
)

REQUIRED_PATHS = (
    "src/multimodal_agent/agent/runtime.py",
    "src/multimodal_agent/agent/conditional_graph.py",
    "src/multimodal_agent/agent/graph_nodes.py",
    "tests/evals/eval_cases.json",
)


def main() -> int:
    missing_imports = [name for name in REQUIRED_IMPORTS if importlib.util.find_spec(name) is None]
    missing_paths = [path for path in REQUIRED_PATHS if not (ROOT / path).exists()]
    result = {
        "python": sys.version.split()[0],
        "missing_imports": missing_imports,
        "missing_paths": missing_paths,
        "ok": not missing_imports and not missing_paths,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
