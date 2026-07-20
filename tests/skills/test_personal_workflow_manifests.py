import json
from pathlib import Path

from assistant_agent.services.tool_workflow_skill import validate_workflow_skill_manifest
from assistant_agent.tools.registry import create_default_registry


def test_personal_workflow_manifests_validate_against_default_registry() -> None:
    registry = create_default_registry()
    manifest_paths = sorted((Path("skills") / "workflows").glob("*.json"))

    assert [path.name for path in manifest_paths] == [
        "capture_action_items.json",
        "morning_briefing.json",
        "schedule_meeting.json",
    ]

    for path in manifest_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = validate_workflow_skill_manifest(payload, registry=registry)

        assert result.accepted is True, (path, [issue.model_dump() for issue in result.issues])
        assert result.manifest is not None
        assert all(permission.startswith("tool:") for permission in result.manifest.permissions)
