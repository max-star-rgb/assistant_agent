"""PyCharm-runnable fixed-input smoke for load_skill_reference."""

from _smoke_runner import PROJECT_ROOT, run_tool_smoke

from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_reference_tool,
)


SKILL_ROOT = PROJECT_ROOT / "evals" / "system" / "tools" / "fixtures" / "skill_repo"
FIXED_INPUT = {
    "skill_id": "smoke-skill",
    "reference_id": "smoke-reference",
}
FIXED_STATE = {
    "skill_reference_grants": {"smoke-skill": ["smoke-reference"]},
}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            create_load_skill_reference_tool(root=SKILL_ROOT),
            FIXED_INPUT,
            state=FIXED_STATE,
        )
    )
