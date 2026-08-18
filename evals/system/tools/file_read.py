"""PyCharm-runnable fixed-input smoke for file_read."""

from _smoke_runner import PROJECT_ROOT, run_tool_smoke

from assistant_agent.tools.plugins.builtin.local_file_access.tool import LocalFileReadTool


FIXTURE_ROOT = PROJECT_ROOT / "evals" / "system" / "tools" / "fixtures"
FIXED_INPUT = {"path": "sample.txt"}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(LocalFileReadTool(root=FIXTURE_ROOT), FIXED_INPUT)
    )
