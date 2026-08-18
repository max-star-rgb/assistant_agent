"""Governed local Python interpreter Tool."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.ids import PYTHON_INTERPRETER_TOOL_NAME
from assistant_agent.tools.native_boundary import (
    builtin_tool_metadata,
    invoke_native_tool,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.python_execution.models import (
    PythonInterpreterError,
    PythonInterpreterInput,
    PythonInterpreterResult,
)
from assistant_agent.tools.plugins.builtin.python_execution.sandbox import (
    PythonSandbox,
    is_python_interpreter_enabled,
    validate_python_code_safety,
)


def create_python_interpreter_tool(
    sandbox: PythonSandbox | None = None,
    *,
    require_enable_env: bool = True,
) -> BaseTool:
    """Create the native governed Python execution Tool."""

    python_sandbox = sandbox or PythonSandbox()

    @tool(PYTHON_INTERPRETER_TOOL_NAME, response_format="content_and_artifact")
    def python_interpreter(
        code: Annotated[
            str,
            Field(
                min_length=1,
                max_length=12_000,
                description="Python 代码；结构化结果赋给 result。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
        input_data: Annotated[
            Any | None,
            Field(description="代码通过 input_data 访问的 JSON 数据。"),
        ] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """在短生命周期的受限 Python 沙箱中执行计算或数据分析代码。

        可通过 input_data 接收 JSON，并返回 result、标准输出、标准错误和截断或
        超时状态。禁止文件、网络、进程、Shell 和动态代码执行。
        """

        del runtime
        error = validate_python_code_safety(code)
        if error is not None:
            raise ToolException(f"{error.code}: {error.message}")
        request = PythonInterpreterInput(code=code, input_data=input_data)
        return invoke_native_tool(
            PYTHON_INTERPRETER_TOOL_NAME,
            lambda: _execute_python_interpreter(
                python_sandbox,
                request,
                require_enable_env=require_enable_env,
            ),
        )

    python_interpreter.metadata = builtin_tool_metadata("write")
    return python_interpreter


def _execute_python_interpreter(
    sandbox: PythonSandbox,
    input: PythonInterpreterInput,
    *,
    require_enable_env: bool,
) -> ToolResult:
    if require_enable_env and not is_python_interpreter_enabled():
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
        result = sandbox.run(input)
    return _tool_result(PYTHON_INTERPRETER_TOOL_NAME, result)


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
