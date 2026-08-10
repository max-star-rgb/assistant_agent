from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import BaseModel

from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.contracts import ToolRegistrationRecord
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.registry_overlay import (
    EvalToolReplacement,
    apply_tool_replacements,
)


class ProbeInput(BaseModel):
    value: str


class ProbeOutput(BaseModel):
    value: str


class ProbeTool(ToolBase):
    name = "probe"
    description = "Run one probe."
    input_schema = ProbeInput
    output_schema = ProbeOutput
    category = "read"
    repeat_policy = "distinct_inputs"

    def __init__(self, result: str) -> None:
        self.result = result

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        del input, context
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": self.result},
        )


class ChangedProbeTool(ProbeTool):
    description = "Changed model-visible description."


def _registration() -> ToolRegistrationRecord:
    return ToolRegistrationRecord(
        tool_name="probe",
        plugin_id="probe.plugin",
        plugin_version="1",
        source_type="manual",
        source_ref="tests:production-probe",
    )


def _production_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ProbeTool("live"), _registration())
    registry.seal()
    return registry


def _replacement(tool: ToolBase | None = None) -> EvalToolReplacement:
    return EvalToolReplacement(
        tool_name="probe",
        tool=tool or ProbeTool("controlled"),
        reason="deterministic provider failure",
        source_ref="tests:controlled-probe",
    )


def test_empty_overlay_preserves_production_catalog_and_live_provenance() -> None:
    production = _production_registry()

    assembly = apply_tool_replacements(production, [])

    assert assembly.registry is not production
    assert assembly.registry.sealed is True
    assert assembly.registry.list() == ["probe"]
    assert assembly.registry.get("probe") is production.get("probe")
    assert assembly.registry.get_spec("probe") == production.get_spec("probe")
    assert assembly.registry.registration_record("probe") == _registration()
    assert assembly.provenance["probe"].model_dump() == {
        "dependency_mode": "live",
        "production_source_type": "manual",
        "production_source_ref": "tests:production-probe",
        "replacement_source_ref": None,
        "replacement_reason": None,
    }


def test_exact_replacement_preserves_catalog_and_changes_implementation() -> None:
    production = _production_registry()
    controlled = ProbeTool("controlled")

    assembly = apply_tool_replacements(
        production,
        [_replacement(controlled)],
    )

    assert assembly.registry.list() == ["probe"]
    assert assembly.registry.get_spec("probe") == production.get_spec("probe")
    assert assembly.registry.get("probe") is controlled
    assert assembly.registry.run("probe", {"value": "input"}).data == {
        "value": "controlled"
    }
    assert assembly.provenance["probe"].model_dump() == {
        "dependency_mode": "controlled_replacement",
        "production_source_type": "manual",
        "production_source_ref": "tests:production-probe",
        "replacement_source_ref": "tests:controlled-probe",
        "replacement_reason": "deterministic provider failure",
    }


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (
            replace(_replacement(), tool_name="missing"),
            "unknown production tools",
        ),
        (
            replace(_replacement(), reason=" "),
            "reason must be non-empty",
        ),
        (
            replace(_replacement(), source_ref=""),
            "source_ref must be non-empty",
        ),
    ],
)
def test_overlay_rejects_invalid_replacement_declarations(
    replacement: EvalToolReplacement,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        apply_tool_replacements(_production_registry(), [replacement])


def test_overlay_rejects_duplicate_replacement_names() -> None:
    with pytest.raises(ValueError, match="duplicate replacement tools"):
        apply_tool_replacements(
            _production_registry(),
            [_replacement(), _replacement()],
        )


def test_overlay_rejects_replacement_tool_name_mismatch() -> None:
    mismatched = ProbeTool("controlled")
    mismatched.name = "other"

    with pytest.raises(ValueError, match="does not match replacement tool name"):
        apply_tool_replacements(
            _production_registry(),
            [_replacement(mismatched)],
        )


def test_overlay_rejects_model_visible_tool_spec_changes() -> None:
    with pytest.raises(ValueError, match="changes ToolSpec"):
        apply_tool_replacements(
            _production_registry(),
            [_replacement(ChangedProbeTool("controlled"))],
        )


def test_failed_overlay_does_not_mutate_production_registry() -> None:
    production = _production_registry()
    generation = production.generation
    original_tool = production.get("probe")

    with pytest.raises(ValueError, match="changes ToolSpec"):
        apply_tool_replacements(
            production,
            [_replacement(ChangedProbeTool("controlled"))],
        )

    assert production.sealed is True
    assert production.generation == generation
    assert production.list() == ["probe"]
    assert production.get("probe") is original_tool
