"""Temporary RED/GREEN coverage for native Skill loading and Tool exposure."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.planning_phase import (
    _planner_capability_catalog,
)
from assistant_agent.native_agent.state import FastAgentState
from assistant_agent.tools.native_boundary import configure_builtin_tool
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_tool,
)


def test_load_skill_updates_state_through_native_toolnode_without_tool_grants(
    tmp_path: Path,
) -> None:
    """Catches Tool exposure owning Skill state or leaking capability grants."""

    _write_skill(tmp_path)
    load_skill = create_load_skill_tool(root=tmp_path)
    builder = StateGraph(FastAgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([load_skill]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "load_skill",
                                "args": {"skill_id": "travel-sentinel"},
                                "id": "load-skill-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            context=AssistantRunContext(),
        )
    )

    assert result["active_skill_ids"] == ["travel-sentinel"]
    assert result["skill_reference_grants"] == {"travel-sentinel": ["route-guide"]}
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.name == "load_skill"
    assert message.tool_call_id == "load-skill-call"
    assert message.artifact == {
        "status": "succeeded",
        "skill_id": "travel-sentinel",
        "content": "# 旅行流程",
        "reference_ids": ["route-guide"],
    }
    observation = json.loads(message.content[0]["text"])
    assert observation == {
        "status": "succeeded",
        "summary": "内部工作流已加载。",
        "skill_id": "travel-sentinel",
        "reference_ids": ["route-guide"],
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
