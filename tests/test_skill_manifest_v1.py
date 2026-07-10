from pathlib import Path

from assistant_agent.services.context.skill_loader import load_repo_skill_descriptors


def test_skill_manifest_v1_loads_visibility_and_tests_as_declaration_only(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "calendar_assistant",
        """
---
name: calendar_assistant
description: Calendar capability declaration.
manifest-version: 1
---
## Governed Tools
- mcp.calendar.search_events

## Permissions
- tool:mcp.calendar.search_events

## Visibility
- toolset: personal.calendar
- tags: calendar, search
- enabled_by_default: false
- skill_only: true

## Tests
- calendar.search_events requires query
- missing permission rejects manifest
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.issues == []
    descriptor = catalog.descriptors[0]
    assert descriptor.manifest_version == 1
    assert descriptor.visibility.toolset == "personal.calendar"
    assert descriptor.visibility.tags == ["calendar", "search"]
    assert descriptor.visibility.enabled_by_default is False
    assert descriptor.visibility.skill_only is True
    assert descriptor.tests == [
        "calendar.search_events requires query",
        "missing permission rejects manifest",
    ]


def test_skill_manifest_v1_still_rejects_missing_tool_permission(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "calendar_assistant",
        """
---
name: calendar_assistant
description: Calendar capability declaration.
manifest-version: 1
---
## Governed Tools
- mcp.calendar.search_events

## Visibility
- toolset: personal.calendar
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert catalog.issues[0].code == "missing_tool_permission"


def _write_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")
