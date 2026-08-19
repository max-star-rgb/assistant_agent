"""PyCharm-runnable fixed-input smoke for load_skill."""

from _smoke_runner import PROJECT_ROOT, run_tool_smoke

from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_tool,
)


SKILL_ROOT = PROJECT_ROOT / "evals" / "system" / "tools" / "fixtures" / "skill_repo"
FIXED_INPUT = {"skill_id": "smoke-skill"}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(create_load_skill_tool(root=SKILL_ROOT), FIXED_INPUT)
    )
