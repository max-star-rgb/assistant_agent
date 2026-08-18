"""PyCharm-runnable launcher for every executable Tool system eval in this folder."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parents[2]
_NON_RUNNABLE_FILES = frozenset({"__init__.py", "native_tool.py", "run_all.py"})


def discover_eval_scripts() -> list[Path]:
    """Return runnable Tool eval files in deterministic filename order."""

    return sorted(
        path
        for path in TOOLS_DIR.rglob("*.py")
        if path.name not in _NON_RUNNABLE_FILES and not path.name.startswith("_")
    )


def main() -> int:
    """Run every discovered eval with the active PyCharm interpreter."""

    scripts = discover_eval_scripts()
    if not scripts:
        print(
            json.dumps(
                {"passed": False, "error": "no_tool_eval_scripts_found"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    results: list[dict[str, object]] = []
    for script in scripts:
        print(f"\n===== {script.name} =====", flush=True)
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=PROJECT_ROOT,
            check=False,
        )
        results.append(
            {
                "script": script.name,
                "passed": completed.returncode == 0,
                "return_code": completed.returncode,
            }
        )

    passed = all(bool(item["passed"]) for item in results)
    print(
        "\n"
        + json.dumps(
            {
                "schema_version": "tool_system_eval_batch_v1",
                "passed": passed,
                "script_count": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
