from __future__ import annotations

from evals.agent.contracts import ToolExecution
from evals.agent.registry_overlay import EvalToolProvenance


def test_legacy_tool_execution_defaults_to_live_provenance() -> None:
    execution = ToolExecution(
        name="calendar_search",
        status="succeeded",
    )

    assert execution.dependency_mode == "live"
    assert execution.production_source_ref is None
    assert execution.replacement_source_ref is None


def test_tool_execution_accepts_controlled_replacement_provenance() -> None:
    provenance = EvalToolProvenance(
        dependency_mode="controlled_replacement",
        production_source_type="builtin",
        production_source_ref="calendar_weather_contacts",
        replacement_source_ref="tests:calendar-search",
        replacement_reason="stable calendar fixture",
    )

    execution = ToolExecution(
        name="calendar_search",
        status="succeeded",
        dependency_mode=provenance.dependency_mode,
        production_source_ref=provenance.production_source_ref,
        replacement_source_ref=provenance.replacement_source_ref,
    )

    assert execution.model_dump()["dependency_mode"] == "controlled_replacement"
    assert execution.model_dump()["replacement_source_ref"] == (
        "tests:calendar-search"
    )
