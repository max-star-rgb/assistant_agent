import json
from pathlib import Path

from assistant_agent.services.context.skill_loader import load_repo_skill_descriptors


def test_load_repo_skill_descriptors_discovers_repo_skill(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Look up current web information with governed search.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- web_search

## Required Inputs
- web_search: query, recency_days

## When To Use
- User asks for current news.

## When Not To Use
- User asks for stored personal preferences.

## Safe Examples
- latest AI industry news

## Runtime Constraints
- Read-only lookup.
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.issues == []
    assert [descriptor.name for descriptor in catalog.descriptors] == ["realtime_web_search"]
    descriptor = catalog.descriptors[0]
    assert descriptor.description == "Look up current web information with governed search."
    assert descriptor.governed_tools == ["web_search"]
    assert descriptor.required_inputs_by_tool == {"web_search": ["query", "recency_days"]}
    assert descriptor.when_to_use == ["User asks for current news."]
    assert descriptor.when_not_to_use == ["User asks for stored personal preferences."]
    assert descriptor.safe_examples == ["latest AI industry news"]
    assert descriptor.runtime_constraints == ["Read-only lookup."]


def test_load_repo_skill_descriptors_ignores_codex_skills(tmp_path: Path) -> None:
    _write_codex_skill(
        tmp_path,
        "codex_workflow",
        """
---
name: codex_workflow
description: This is a Codex workflow skill, not a product runtime skill.
---
## Governed Tools
- web_search
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert catalog.issues == []


def test_load_repo_skill_descriptors_skips_name_mismatch(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "directory_name",
        """
---
name: frontmatter_name
description: Names must match the containing directory.
---
## Governed Tools
- web_search
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["name_mismatch"]


def test_load_repo_skill_descriptors_skips_disabled_skill(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "disabled_skill",
        """
---
name: disabled_skill
description: Disabled skills are not prompt-visible.
enabled: false
---
## Governed Tools
- web_search
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["skill_disabled"]


def test_load_repo_skill_descriptors_skips_model_invocation_disabled_skill(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "manual_only",
        """
---
name: manual_only
description: Manual-only docs must not be injected into the model context.
disable-model-invocation: true
---
## Governed Tools
- web_search
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["model_invocation_disabled"]


def test_load_repo_skill_descriptors_skips_missing_description(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "missing_description",
        """
---
name: missing_description
---
## Governed Tools
- web_search
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["missing_description"]


def test_load_repo_skill_descriptors_skips_missing_governed_tools(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "missing_governed_tools",
        """
---
name: missing_governed_tools
description: A skill without governed tools cannot grant any execution ability.
---
## When To Use
- User asks for current news.
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["missing_governed_tools"]


def test_load_repo_skill_descriptors_omits_unallowed_raw_body_steps(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "safe_search",
        """
---
name: safe_search
description: Search safely through the governed web_search tool.
---
## Governed Tools
- web_search

## When To Use
- User asks for current news.

## Steps
- Run shell: curl https://example.test/private
- Open browser and scrape all pages.
""",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert [descriptor.name for descriptor in catalog.descriptors] == ["safe_search"]
    rendered = json.dumps(catalog.descriptors[0].model_dump(mode="json"), ensure_ascii=False)
    assert "curl" not in rendered
    assert "browser" not in rendered
    assert "## Steps" not in rendered


def _write_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")


def _write_codex_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / ".codex" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")
