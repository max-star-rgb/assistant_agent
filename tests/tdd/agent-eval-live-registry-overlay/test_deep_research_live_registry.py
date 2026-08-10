from __future__ import annotations

from assistant_agent.config import ProviderConfig
from evals.agent.deep_research_support import DeepResearchMissionEnvironment


def test_deep_research_uses_empty_overlay_and_no_private_workflow_store() -> None:
    environment = DeepResearchMissionEnvironment(
        config=ProviderConfig(provider_mode="mock"),
    )

    assert environment.tool_replacements(None) == ()
    assert environment.runtime_assembly is None
    assert "workflow_store" not in vars(environment)
    assert "_tempdir" not in vars(environment)
    assert not hasattr(type(environment), "build_registry")
    assert environment.describe()["dependencies"] == "live:production-workflow"
    assert environment.describe()["state_reset"] == "persistent_production_store"
