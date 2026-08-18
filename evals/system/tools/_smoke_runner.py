"""Shared runner for PyCharm-runnable Tool execution smokes."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC = PROJECT_ROOT / "src"
for candidate in (PROJECT_ROOT, SRC):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from langchain_core.tools import BaseTool

from evals.system.tools.native_tool import invoke_native_tool


def run_tool_smoke(
    tool: BaseTool,
    fixed_input: dict[str, Any],
    *,
    request_content: str | list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    cleanup: Callable[[], None] | None = None,
) -> int:
    """Run one Tool once and report only execution/return-contract health."""

    run_id = f"tool-smoke-{tool.name}-{uuid4().hex}"
    try:
        invocation = invoke_native_tool(
            tool,
            fixed_input,
            user_identity="tool-smoke-user",
            thread_id=run_id,
            tool_call_id=f"{tool.name}-fixed-input",
            request_content=request_content,
            state=state,
        )
        artifact = invocation.artifact
        result_returned = bool(artifact) or bool(invocation.message.content)
        passed = invocation.status == "succeeded" and result_returned
        output = {
            "schema_version": "tool_execution_smoke_v1",
            "tool_name": tool.name,
            "fixed_input": fixed_input,
            "passed": passed,
            "tool_call_status": invocation.status,
            "result_returned": result_returned,
            "artifact_keys": sorted(artifact),
        }
    except Exception as exc:  # noqa: BLE001 - the smoke must expose any Tool failure
        passed = False
        output = {
            "schema_version": "tool_execution_smoke_v1",
            "tool_name": tool.name,
            "fixed_input": fixed_input,
            "passed": False,
            "tool_call_status": "raised",
            "result_returned": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    finally:
        if cleanup is not None:
            cleanup()

    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0 if passed else 1


__all__ = ["PROJECT_ROOT", "run_tool_smoke"]
