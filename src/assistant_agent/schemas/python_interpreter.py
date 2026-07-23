"""Schemas for the governed local Python interpreter tool."""

from typing import Any, Literal

from pydantic import BaseModel, Field


PYTHON_INTERPRETER_ENABLED_ENV = "MULTIMODAL_AGENT_PYTHON_INTERPRETER_ENABLED"
PYTHON_INTERPRETER_MAX_CODE_CHARS = 12_000
PYTHON_INTERPRETER_MAX_INPUT_CHARS = 40_000
PYTHON_INTERPRETER_MAX_STDOUT_CHARS = 4_000
PYTHON_INTERPRETER_MAX_STDERR_CHARS = 2_000
PYTHON_INTERPRETER_MAX_RESULT_CHARS = 4_000
PYTHON_INTERPRETER_DEFAULT_TIMEOUT_S = 3
PYTHON_INTERPRETER_MAX_TIMEOUT_S = 10

PythonInterpreterPurpose = Literal[
    "general_analysis",
    "math_analysis",
    "scientific_analysis",
    "code_analysis",
    "data_analysis",
]
PythonInterpreterStatus = Literal["succeeded", "failed", "timeout", "rejected"]


class PythonInterpreterInput(BaseModel):
    """受限本地 Python 分析任务的输入。"""

    code: str = Field(
        min_length=1,
        max_length=PYTHON_INTERPRETER_MAX_CODE_CHARS,
        description=(
            "用于本地分析的 Python 代码。需要结构化输出时，"
            "请将最终可 JSON 序列化的值赋给变量 result。"
        ),
    )
    purpose: PythonInterpreterPurpose = Field(
        default="general_analysis",
        description="分析用途：数学、科学、代码、数据或通用分析。",
    )
    input_data: Any | None = Field(
        default=None,
        description="可选的 JSON 可序列化数据，代码中通过 input_data 访问。",
    )
    timeout_s: int | None = Field(
        default=None,
        ge=1,
        le=PYTHON_INTERPRETER_MAX_TIMEOUT_S,
        description="可选执行超时秒数，最终值受沙箱上限约束。",
    )


class PythonInterpreterError(BaseModel):
    """Stable error item returned by the Python sandbox."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class PythonInterpreterResult(BaseModel):
    """Structured output from a local Python analysis run."""

    status: PythonInterpreterStatus
    stdout: str = ""
    stderr: str = ""
    result_json: Any | None = None
    result_repr: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    errors: list[PythonInterpreterError] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
