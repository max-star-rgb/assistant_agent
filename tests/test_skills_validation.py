from pathlib import Path

from scripts.validate_skills import validate_skills


def test_repository_skills_validate_offline() -> None:
    result = validate_skills(Path("skills"))

    assert result["ok"] is True
    assert "assistant-demo-flow" in result["skills"]
    assert "offline-mcp-tools" in result["skills"]


def test_packaged_skills_have_runbook_resources() -> None:
    assert Path("skills/assistant-demo-flow/resources/demo-runbook.md").exists()
    assert Path("skills/assistant-demo-flow/resources/demo-scenarios.md").exists()
    assert Path("skills/offline-mcp-tools/resources/mcp-smoke-runbook.md").exists()
    assert Path("skills/offline-mcp-tools/resources/mcp-tool-inventory.md").exists()
