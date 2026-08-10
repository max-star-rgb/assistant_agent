from __future__ import annotations

import pytest

from assistant_agent.tools.registry import ToolRegistry
from evals.release_review.catalog import build_catalog_snapshot
from tests.core.support import ProbeTool


class AlphaTool(ProbeTool):
    name = "alpha_tool"
    description = "alpha"


class ZetaTool(ProbeTool):
    name = "zeta_tool"
    description = "zeta"


def _registry(*tools: ProbeTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    registry.seal()
    return registry


def test_catalog_generation_is_stable_across_registration_order() -> None:
    first = build_catalog_snapshot(_registry(ZetaTool(), AlphaTool()))
    second = build_catalog_snapshot(_registry(AlphaTool(), ZetaTool()))

    assert first.generation == second.generation
    assert first.tool_names == ("alpha_tool", "zeta_tool")
    assert [spec.name for spec in first.tools] == ["alpha_tool", "zeta_tool"]


def test_catalog_preflight_rejects_missing_required_tools() -> None:
    snapshot = build_catalog_snapshot(_registry(AlphaTool()))

    with pytest.raises(ValueError, match=r"missing required tools: missing_tool"):
        snapshot.require_tools(["alpha_tool", "missing_tool"])


def test_catalog_requires_a_sealed_registry() -> None:
    registry = ToolRegistry()
    registry.register(AlphaTool())

    with pytest.raises(ValueError, match="sealed"):
        build_catalog_snapshot(registry)
