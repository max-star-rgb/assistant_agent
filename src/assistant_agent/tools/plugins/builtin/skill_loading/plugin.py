"""Built-in project Skill loading plugin."""

from assistant_agent.skills.loading import (
    default_repo_root,
    load_repo_skill_descriptors,
)
from langchain_core.tools import BaseTool
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    LoadSkillReferenceTool,
    LoadSkillTool,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


class SkillLoadingPlugin:
    def build_tools(self, context: ToolPluginContext) -> list[BaseTool]:
        root = default_repo_root()
        if not load_repo_skill_descriptors(root).descriptors:
            return []
        return [
            LoadSkillTool(root=root),
            LoadSkillReferenceTool(root=root),
        ]
