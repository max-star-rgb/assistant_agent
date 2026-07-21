"""Governed local Python interpreter tool."""

from __future__ import annotations

from typing import Any

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.python_interpreter import (
    PythonInterpreterError,
    PythonInterpreterInput,
    PythonInterpreterResult,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.tool_python_sandbox import (
    PythonSandbox,
    is_python_interpreter_enabled,
    validate_python_code_safety,
)
from assistant_agent.services.tool_manifest import PYTHON_INTERPRETER_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext, ToolInputValidationError


class PythonInterpreterTool(ToolBase):
    name = PYTHON_INTERPRETER_TOOL_NAME
    description = (
        "Run short, local, restricted Python code for math, scientific, data, "
        "or code analysis."
    )
    input_schema = PythonInterpreterInput
    output_schema = PythonInterpreterResult
    category = "dangerous"
    toolset = "analysis.local"
    requires_confirmation = False
    requires_env = ["MULTIMODAL_AGENT_PYTHON_INTERPRETER_ENABLED"]
    enabled_by_default = False
    progress_message = "我用本地 Python 算一下。"

    def __init__(
        self,
        sandbox: PythonSandbox | None = None,
        *,
        require_enable_env: bool = True,
    ) -> None:
        self.sandbox = sandbox or PythonSandbox()
        self.require_enable_env = require_enable_env

    def validate_call(self, input: PythonInterpreterInput) -> None:
        """Reject unsafe code at the tool-owned pre-execution boundary."""

        error = validate_python_code_safety(input.code)
        if error is not None:
            raise ToolInputValidationError(error.code, error.message)

    def _run(self, input: PythonInterpreterInput, context: ToolContext) -> ToolResult:
        if self.require_enable_env and not is_python_interpreter_enabled():
            result = PythonInterpreterResult(
                status="rejected",
                errors=[
                    PythonInterpreterError(
                        code="python_interpreter_disabled",
                        message="Python interpreter tool is not enabled.",
                    )
                ],
            )
        else:
            result = self.sandbox.run(input)
        return _tool_result(self.name, result)


def _tool_result(tool_name: str, result: PythonInterpreterResult) -> ToolResult:
    success = result.status == "succeeded" and not result.errors
    data = result.model_dump(mode="json")
    errors = [error.model_dump(mode="json") for error in result.errors]
    summary = _summary(result)
    contract = build_capability_output_contract(
        capability=PYTHON_INTERPRETER_TOOL_NAME,
        status="succeeded" if success else "failed",
        data={
            "status": result.status,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "result_json": result.result_json,
            "result_repr": result.result_repr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        },
        errors=errors,
        metadata={"latency_ms": result.latency_ms, "exit_code": result.exit_code},
    )
    return ToolResult(
        tool_name=tool_name,
        success=success,
        data={**data, "contract": contract.model_dump(mode="json")},
        model_observation=_model_observation(result, summary=summary),
        trace_summary={
            "summary": summary,
            "status": result.status,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "stdout_chars": len(result.stdout),
            "stderr_chars": len(result.stderr),
            "error_count": len(result.errors),
        },
        audit_payload={"status": result.status, "redacted": True},
        error=_error_message(result),
        latency_ms=result.latency_ms,
        contract=contract,
    )


def _model_observation(
    result: PythonInterpreterResult,
    *,
    summary: str,
) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "status": result.status,
        "summary": summary,
    }
    if result.result_json is not None:
        observation["result"] = result.result_json
    elif result.result_repr:
        observation["result_repr"] = result.result_repr
    if result.stdout:
        observation["stdout"] = result.stdout
        observation["stdout_chars"] = len(result.stdout)
    if result.stderr:
        observation["stderr"] = result.stderr
        observation["stderr_chars"] = len(result.stderr)
    if result.timed_out:
        observation["timed_out"] = True
    if result.truncated:
        observation["truncated"] = True
    if result.errors:
        observation["errors"] = [
            {
                "code": error.code,
                "message": error.message,
            }
            for error in result.errors
        ]
    return observation


def _summary(result: PythonInterpreterResult) -> str:
    if result.status == "succeeded":
        return "Python analysis succeeded."
    if result.errors:
        return result.errors[0].message
    if result.status == "timeout":
        return "Python execution timed out."
    return "Python analysis failed."


def _error_message(result: PythonInterpreterResult) -> str | None:
    if result.status == "succeeded" and not result.errors:
        return None
    if result.errors:
        first = result.errors[0]
        return f"{first.code}: {first.message}"
    return "python_execution_failed: Python analysis failed."
