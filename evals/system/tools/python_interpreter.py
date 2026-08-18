"""PyCharm-runnable fixed-input smoke for python_interpreter."""

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.python_execution.tool import PythonInterpreterTool


FIXED_INPUT = {"code": "result = 1 + 1"}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            PythonInterpreterTool(require_enable_env=False),
            FIXED_INPUT,
        )
    )
