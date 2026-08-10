from __future__ import annotations

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.registry_overlay import apply_tool_replacements


def test_runtime_transforms_default_registry_before_executor_binding() -> None:
    seen: list[ToolRegistry] = []

    def transform(production: ToolRegistry) -> ToolRegistry:
        seen.append(production)
        return apply_tool_replacements(production, []).registry

    runtime = AgentGraphRuntime(
        config=ProviderConfig(provider_mode="mock"),
        registry_transform=transform,
    )
    try:
        assert len(seen) == 1
        assert seen[0].sealed is True
        assert runtime.registry is not seen[0]
        assert runtime.registry.list() == seen[0].list()
        assert runtime.registry.generation == seen[0].generation
        assert runtime.tool_executor.registry is runtime.registry
    finally:
        runtime.close()


def test_runtime_rejects_registry_and_transform_together() -> None:
    registry = ToolRegistry()
    registry.seal()

    with pytest.raises(
        ValueError,
        match="registry and registry_transform are mutually exclusive",
    ):
        AgentGraphRuntime(
            config=ProviderConfig(provider_mode="mock"),
            registry=registry,
            registry_transform=lambda production: production,
        )


def test_runtime_rejects_non_registry_transform_result() -> None:
    with pytest.raises(TypeError, match="registry_transform must return ToolRegistry"):
        AgentGraphRuntime(
            config=ProviderConfig(provider_mode="mock"),
            registry_transform=lambda production: object(),  # type: ignore[arg-type]
        )


def test_runtime_rejects_unsealed_transform_result() -> None:
    with pytest.raises(
        ValueError,
        match="registry_transform must return a sealed ToolRegistry",
    ):
        AgentGraphRuntime(
            config=ProviderConfig(provider_mode="mock"),
            registry_transform=lambda production: ToolRegistry(),
        )
