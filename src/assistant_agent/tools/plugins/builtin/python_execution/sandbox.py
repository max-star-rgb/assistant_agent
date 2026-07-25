"""Plugin-private guarded subprocess runner for the Python Tool."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from time import perf_counter
from typing import Any

from assistant_agent.schemas.python_interpreter import (
    PYTHON_INTERPRETER_DEFAULT_TIMEOUT_S,
    PYTHON_INTERPRETER_ENABLED_ENV,
    PYTHON_INTERPRETER_MAX_INPUT_CHARS,
    PYTHON_INTERPRETER_MAX_RESULT_CHARS,
    PYTHON_INTERPRETER_MAX_STDERR_CHARS,
    PYTHON_INTERPRETER_MAX_STDOUT_CHARS,
    PYTHON_INTERPRETER_MAX_TIMEOUT_S,
    PythonInterpreterError,
    PythonInterpreterInput,
    PythonInterpreterResult,
)
from assistant_agent.services.provider_errors import sanitize_error_message


ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "ast",
        "bisect",
        "calendar",
        "collections",
        "copy",
        "csv",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "heapq",
        "itertools",
        "json",
        "math",
        "operator",
        "re",
        "statistics",
        "string",
        "textwrap",
        "typing",
    }
)
_BLOCKED_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)
_BLOCKED_ATTRS = frozenset(
    {
        "chmod",
        "chown",
        "connect",
        "exec",
        "fork",
        "mkdir",
        "makedirs",
        "open",
        "popen",
        "read",
        "read_bytes",
        "read_text",
        "remove",
        "rename",
        "replace",
        "request",
        "rmdir",
        "send",
        "spawn",
        "system",
        "unlink",
        "urlopen",
        "write",
        "write_bytes",
        "write_text",
    }
)
_RUNNER_FILENAME = "assistant_python_runner.py"
_PAYLOAD_FILENAME = "payload.json"
_RESULT_FILENAME = "result.json"


class PythonSandbox:
    """Execute already-validated Python snippets in a short-lived subprocess."""

    def run(self, request: PythonInterpreterInput) -> PythonInterpreterResult:
        started = perf_counter()
        safety_error = validate_python_code_safety(request.code)
        if safety_error is not None:
            return _result(
                "rejected",
                [safety_error],
                started=started,
            )

        payload_result = _json_payload(request)
        if isinstance(payload_result, PythonInterpreterError):
            return _result("rejected", [payload_result], started=started)
        payload = payload_result
        timeout_s = _timeout_s(request.timeout_s)

        with tempfile.TemporaryDirectory(prefix="assistant-python-") as temp_dir:
            root = Path(temp_dir)
            runner_path = root / _RUNNER_FILENAME
            payload_path = root / _PAYLOAD_FILENAME
            result_path = root / _RESULT_FILENAME
            runner_path.write_text(_RUNNER_SOURCE, encoding="utf-8")
            payload_path.write_text(payload, encoding="utf-8")

            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(runner_path),
                        str(payload_path),
                        str(result_path),
                    ],
                    cwd=temp_dir,
                    env=_subprocess_env(),
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    preexec_fn=_resource_limiter(timeout_s),
                )
            except subprocess.TimeoutExpired:
                return _result(
                    "timeout",
                    [
                        PythonInterpreterError(
                            code="python_execution_timeout",
                            message="Python execution timed out.",
                            recoverable=True,
                        )
                    ],
                    started=started,
                    timed_out=True,
                )

            if not result_path.exists():
                message = completed.stderr or completed.stdout or "Python runner did not return a result."
                return _result(
                    "failed",
                    [
                        PythonInterpreterError(
                            code="python_execution_failed",
                            message=sanitize_error_message(message),
                            recoverable=True,
                        )
                    ],
                    started=started,
                    exit_code=completed.returncode,
                )

            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return _result(
                    "failed",
                    [
                        PythonInterpreterError(
                            code="python_result_schema_mismatch",
                            message=sanitize_error_message(exc),
                            recoverable=True,
                        )
                    ],
                    started=started,
                    exit_code=completed.returncode,
                )

        return _result_from_runner_payload(
            data,
            started=started,
            exit_code=completed.returncode,
        )


def is_python_interpreter_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the local Python interpreter is explicitly enabled."""

    source = os.environ if env is None else env
    value = str(source.get(PYTHON_INTERPRETER_ENABLED_ENV, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def validate_python_code_safety(code: str) -> PythonInterpreterError | None:
    """Reject obvious file, network, process, shell, and introspection access."""

    if not isinstance(code, str) or not code.strip():
        return PythonInterpreterError(
            code="invalid_tool_input",
            message="python_interpreter requires code.",
        )
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return PythonInterpreterError(
            code="python_syntax_error",
            message=sanitize_error_message(f"{exc.__class__.__name__}: {exc.msg}"),
            recoverable=True,
        )
    checker = _PythonSafetyChecker()
    checker.visit(tree)
    if checker.error is not None:
        return checker.error
    return None


def _json_payload(request: PythonInterpreterInput) -> str | PythonInterpreterError:
    try:
        payload = json.dumps(
            {
                "code": request.code,
                "input_data": request.input_data,
                "allowed_import_roots": sorted(ALLOWED_IMPORT_ROOTS),
                "max_stdout_chars": PYTHON_INTERPRETER_MAX_STDOUT_CHARS,
                "max_stderr_chars": PYTHON_INTERPRETER_MAX_STDERR_CHARS,
                "max_result_chars": PYTHON_INTERPRETER_MAX_RESULT_CHARS,
            },
            ensure_ascii=False,
        )
    except TypeError as exc:
        return PythonInterpreterError(
            code="python_input_not_json_serializable",
            message=sanitize_error_message(exc),
        )
    if len(payload) > PYTHON_INTERPRETER_MAX_INPUT_CHARS:
        return PythonInterpreterError(
            code="python_input_too_large",
            message="Python interpreter input_data is too large.",
        )
    return payload


def _timeout_s(value: int | None) -> int:
    timeout_s = value or PYTHON_INTERPRETER_DEFAULT_TIMEOUT_S
    return max(1, min(timeout_s, PYTHON_INTERPRETER_MAX_TIMEOUT_S))


def _subprocess_env() -> dict[str, str]:
    return {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _resource_limiter(timeout_s: int):
    if os.name != "posix":
        return None

    def limit() -> None:
        try:
            import resource

            cpu_limit = max(timeout_s + 1, 2)
            memory_limit = 256 * 1024 * 1024
            file_limit = 1_000_000
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
        except Exception:
            return

    return limit


def _result(
    status: str,
    errors: list[PythonInterpreterError],
    *,
    started: float,
    stdout: str = "",
    stderr: str = "",
    result_json: Any | None = None,
    result_repr: str | None = None,
    exit_code: int | None = None,
    timed_out: bool = False,
    truncated: bool = False,
) -> PythonInterpreterResult:
    return PythonInterpreterResult(
        status=status,
        stdout=stdout,
        stderr=stderr,
        result_json=result_json,
        result_repr=result_repr,
        exit_code=exit_code,
        timed_out=timed_out,
        truncated=truncated,
        errors=errors,
        latency_ms=int((perf_counter() - started) * 1000),
    )


def _result_from_runner_payload(
    data: dict[str, Any],
    *,
    started: float,
    exit_code: int,
) -> PythonInterpreterResult:
    errors = [
        _error_from_payload(error)
        for error in data.get("errors", [])
        if isinstance(error, dict)
    ]
    status = str(data.get("status") or "failed")
    if status not in {"succeeded", "failed", "timeout", "rejected"}:
        status = "failed"
    return _result(
        status,
        errors,
        started=started,
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
        result_json=data.get("result_json"),
        result_repr=data.get("result_repr"),
        exit_code=exit_code,
        timed_out=bool(data.get("timed_out", False)),
        truncated=bool(data.get("truncated", False)),
    )


def _error_from_payload(error: dict[str, Any]) -> PythonInterpreterError:
    return PythonInterpreterError(
        code=str(error.get("code") or "python_execution_failed"),
        message=sanitize_error_message(error.get("message") or "Python execution failed."),
        recoverable=bool(error.get("recoverable", False)),
    )


class _PythonSafetyChecker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.error: PythonInterpreterError | None = None

    def visit(self, node: ast.AST) -> Any:
        if self.error is not None:
            return None
        return super().visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self._reject()
            return
        self._check_module(node.module or "")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__") or node.id in _BLOCKED_NAMES:
            self._reject()
            return
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") or node.attr in _BLOCKED_ATTRS:
            self._reject()
            return
        self.generic_visit(node)

    def _check_module(self, module: str) -> None:
        root = module.split(".", 1)[0]
        if root not in ALLOWED_IMPORT_ROOTS:
            self._reject()

    def _reject(self) -> None:
        self.error = PythonInterpreterError(
            code="unsafe_tool_input",
            message=(
                "python_interpreter does not allow shell, network, file, process, "
                "or introspection access."
            ),
        )


_RUNNER_SOURCE = r'''
import contextlib
import io
import json
import sys


def main():
    payload_path = sys.argv[1]
    result_path = sys.argv[2]
    with open(payload_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    errors = []
    status = "succeeded"
    result_json = None
    result_repr = None
    namespace = {
        "__builtins__": safe_builtins(set(payload.get("allowed_import_roots") or [])),
        "__name__": "__assistant_python_interpreter__",
        "input_data": payload.get("input_data"),
        "result": None,
    }

    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            exec(compile(payload["code"], "<assistant-python-interpreter>", "exec"), namespace, namespace)
        result_json, result_repr = jsonable_result(namespace.get("result"))
    except BaseException as exc:
        status = "failed"
        errors.append({
            "code": "python_exception",
            "message": f"{type(exc).__name__}: {exc}",
            "recoverable": True,
        })

    stdout, stdout_truncated = clip(stdout_buffer.getvalue(), int(payload.get("max_stdout_chars") or 4000))
    stderr, stderr_truncated = clip(stderr_buffer.getvalue(), int(payload.get("max_stderr_chars") or 2000))
    if result_repr is not None:
        result_repr, result_truncated = clip(result_repr, int(payload.get("max_result_chars") or 4000))
    else:
        result_truncated = False
    output = {
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "result_json": result_json,
        "result_repr": result_repr,
        "timed_out": False,
        "truncated": stdout_truncated or stderr_truncated or result_truncated,
        "errors": errors,
    }
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False)


def safe_builtins(allowed_imports):
    allowed = {
        "ArithmeticError": ArithmeticError,
        "AssertionError": AssertionError,
        "Exception": Exception,
        "KeyError": KeyError,
        "RuntimeError": RuntimeError,
        "TypeError": TypeError,
        "ValueError": ValueError,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "pow": pow,
        "print": print,
        "range": range,
        "repr": repr,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
    }

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = str(name).split(".", 1)[0]
        if level != 0 or root not in allowed_imports:
            raise ImportError(f"Import is not allowed: {name}")
        return __import__(name, globals, locals, fromlist, level)

    allowed["__import__"] = guarded_import
    return allowed


def jsonable_result(value):
    if value is None:
        return None, None
    try:
        json.dumps(value, ensure_ascii=False)
        return value, repr(value)
    except TypeError:
        return None, repr(value)


def clip(value, limit):
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


if __name__ == "__main__":
    main()
'''.lstrip()
