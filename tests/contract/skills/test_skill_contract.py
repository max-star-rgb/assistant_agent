"""Stable public naming contracts for executable Skills."""

import pytest
from pydantic import ValidationError

from assistant_agent.api.app import create_app
from assistant_agent.skills.runtime import SkillManifest


def test_skill_manifest_uses_one_skill_vocabulary() -> None:
    manifest = SkillManifest.model_validate(
        {
            "schema_version": "skill_v1",
            "name": "daily_briefing",
            "type": "skill",
            "steps": [{"id": "weather", "tool": "weather"}],
        }
    )

    assert manifest.name == "daily_briefing"
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(
            {
                "schema_version": "workflow_skill_v1",
                "name": "legacy",
                "type": "workflow",
                "steps": [{"id": "weather", "tool": "weather"}],
            }
        )


def test_skill_http_routes_do_not_expose_workflow_aliases() -> None:
    paths = {route.path for route in create_app().routes}

    assert "/skills" in paths
    assert "/skills/{skill_id}/runs" in paths
    assert "/skill-runs/{run_id}" in paths
    assert all("workflow" not in path for path in paths)
