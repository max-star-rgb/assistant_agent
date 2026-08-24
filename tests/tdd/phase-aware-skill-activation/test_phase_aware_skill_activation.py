"""Temporary RED/GREEN coverage for phase-aware Skill activation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool
from pydantic import Field

from assistant_agent.native_agent.planning_phase import (
    _planner_capability_catalog,
)
from assistant_agent.tools.native_boundary import configure_builtin_tool
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    _execute_load_skill,
)
from assistant_agent.tools.plugins.builtin.skill_loading.models import (
    LoadSkillRequest,
)


def test_load_skill_returns_phase_aware_capability_activation(tmp_path: Path) -> None:
    """Catches Skill loading presenting governed Tools as immediately callable."""

    _write_skill(tmp_path)

    result = _execute_load_skill(
        tmp_path,
        LoadSkillRequest(skill_id="travel-sentinel"),
    )

    assert result.success is True
    assert result.model_observation == {
        "status": "succeeded",
        "summary": "专业流程与分阶段能力已激活。",
        "skill_id": "travel-sentinel",
        "reference_ids": ["route-guide"],
        "capability_activation": {
            "projection": "phase_aware",
            "tool_names": ["route_probe"],
        },
        "unavailable_tools": [],
    }
    assert result.data is not None
    assert result.data["capability_activation"] == {
        "projection": "phase_aware",
        "tool_names": ["route_probe"],
    }


def test_planner_catalog_describes_inputs_and_result_channels() -> None:
    """Catches planner delegation losing the Tool contract needed to split work."""

    @tool("route_probe", response_format="content_and_artifact")
    def route_probe(
        origin: Annotated[str, Field(description="出发地")],
        destination: Annotated[str, Field(description="目的地")],
        alternatives: Annotated[int, Field(ge=1, le=3)] = 1,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """查询两个地点之间的路线。"""

        return ([{"type": "text", "text": "route"}], {"route": "sentinel"})

    configured = configure_builtin_tool(route_probe, "read")

    catalog = _planner_capability_catalog(
        [configured],
        allowed_names=None,
    )

    assert catalog == (
        {
            "name": "route_probe",
            "effect": "read",
            "purpose": "查询两个地点之间的路线。",
            "required_inputs": ["destination", "origin"],
            "result_channels": ["content", "artifact"],
        },
    )


def _write_skill(root: Path) -> None:
    skill_dir = root / "skills" / "travel-sentinel"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "skill.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                'skill_id = "travel-sentinel"',
                "version = 1",
                'description = "旅行规划"',
                "enabled = true",
                "discoverable = true",
                "disable_model_invocation = false",
                'activation = "model"',
                'governed_tools = ["route_probe"]',
                "",
                "[references]",
                'route-guide = "references/route-guide.md"',
            ]
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# 旅行流程\n", encoding="utf-8")
    (references_dir / "route-guide.md").write_text(
        "# 路线参考\n",
        encoding="utf-8",
    )
