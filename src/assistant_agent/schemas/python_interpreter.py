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
    """Input for a restricted local Python analysis run."""

    code: str = Field(
        min_length=1,
        max_length=PYTHON_INTERPRETER_MAX_CODE_CHARS,
        description=(
            "Python code to run for local analysis. Assign a final JSON-serializable "
            "value to the variable result when the answer needs structured output."
        ),
    )
    purpose: PythonInterpreterPurpose = Field(
        default="general_analysis",
        description="The analysis purpose: math, scientific, code, data, or general.",
    )
    input_data: Any | None = Field(
        default=None,
        description="Optional JSON-serializable data exposed to the code as input_data.",
    )
    timeout_s: int | None = Field(
        default=None,
        ge=1,
        le=PYTHON_INTERPRETER_MAX_TIMEOUT_S,
        description="Optional execution timeout in seconds, clamped by the sandbox.",
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
