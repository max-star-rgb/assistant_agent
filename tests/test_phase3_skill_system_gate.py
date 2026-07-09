from pathlib import Path

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.context.capability_catalog import select_tool_capability_descriptors
from assistant_agent.services.context.skill_loader import load_repo_skill_descriptors
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.tools.registry import create_default_registry


def test_phase3_skill_manifest_declares_permissions_and_tool_mapping(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Search current information through governed web_search.
enabled: true
---
## Governed Tools
- web_search

## Permissions
- tool:web_search

## Required Inputs
- web_search: query

## When To Use
- User asks for latest or current information.
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    selected = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert [skill.name for skill in selected.capabilities] == ["realtime_web_search"]
    descriptor = selected.capabilities[0]
    assert descriptor.governed_tools == ["web_search"]
    assert descriptor.permissions == ["tool:web_search"]
    assert descriptor.required_inputs_by_tool == {"web_search": ["query"]}
    assert any("ToolExecutor" in item for item in descriptor.runtime_constraints)
    assert selected.skill_report.schema_version == "skill_report_v1"
    assert selected.skill_report.selected_skill_ids == ["realtime_web_search"]
    assert selected.skill_report.governed_tool_names == ["web_search"]


def test_phase3_disabled_and_missing_permission_skills_are_audited(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "disabled_search",
        """
---
name: disabled_search
description: Disabled skills are not prompt visible.
enabled: false
---
## Governed Tools
- web_search

## Permissions
- tool:web_search
""",
    )
    _write_skill(
        tmp_path,
        "missing_permission",
        """
---
name: missing_permission
description: Missing permissions should prevent prompt injection.
---
## Governed Tools
- web_search
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [(issue.skill_id, issue.code) for issue in catalog.issues] == [
        ("disabled_search", "skill_disabled"),
        ("missing_permission", "missing_tool_permission"),
    ]


def test_phase3_skill_without_permission_never_reaches_prompt_catalog(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "unsafe_search",
        """
---
name: unsafe_search
description: Missing tool permission should omit the descriptor.
---
## Governed Tools
- web_search
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    selected = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert "unsafe_search" not in [skill.name for skill in selected.capabilities]
    assert "capability_catalog_skill_issue:unsafe_search:missing_tool_permission" in selected.selection_reasons
    assert selected.skill_report.permission_issue_count == 1
    assert selected.skill_report.skipped[0].reason == "missing_tool_permission"


def test_phase3_invalid_permission_never_reaches_prompt_catalog(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "unsafe_search",
        """
---
name: unsafe_search
description: Unknown permission vocabulary should omit the descriptor.
---
## Governed Tools
- web_search

## Permissions
- tool:web_search
- shell:run
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    selected = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert "unsafe_search" not in [skill.name for skill in selected.capabilities]
    assert "capability_catalog_skill_issue:unsafe_search:invalid_permission" in selected.selection_reasons
    assert selected.skill_report.permission_issue_count == 1


def test_phase3_skill_system_does_not_create_direct_execution_path() -> None:
    registry = create_default_registry()
    source = Path("src/assistant_agent/services/context/skill_loader.py").read_text(encoding="utf-8")
    catalog_source = Path("src/assistant_agent/services/context/capability_catalog.py").read_text(
        encoding="utf-8"
    )

    assert "run_skill" not in registry.list()
    assert "run_skill" not in source
    assert "registry.run(" not in source
    assert "registry.run(" not in catalog_source


def _write_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")
