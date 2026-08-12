from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _AssertionOperators(_StrictModel):
    path: str = Field(min_length=1)
    equals: Any | None = None
    contains: Any | None = None
    gte: int | float | None = None
    exists: bool | None = None
    length: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _require_exactly_one_operator(self) -> Self:
        operators = ("equals", "contains", "gte", "exists", "length")
        active = [name for name in operators if getattr(self, name) is not None]
        if len(active) != 1:
            raise ValueError("assertion must define exactly one operator")
        return self


class ToolArgumentAssertion(_AssertionOperators):
    tool: str = Field(min_length=1)


class StateAssertion(_AssertionOperators):
    pass


class ToolSequenceContract(_StrictModel):
    before: tuple[tuple[str, str], ...] = ()
    before_final_response: tuple[str, ...] = ()


class ToolContract(_StrictModel):
    required: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    arguments: tuple[ToolArgumentAssertion, ...] = ()
    sequence: ToolSequenceContract = Field(default_factory=ToolSequenceContract)

    @model_validator(mode="after")
    def _validate_tool_sets(self) -> Self:
        groups = {
            "required": self.required,
            "allowed": self.allowed,
            "forbidden": self.forbidden,
        }
        for label, names in groups.items():
            if len(names) != len(set(names)):
                raise ValueError(f"{label} tools must be unique")
        forbidden_conflicts = (set(self.required) | set(self.allowed)) & set(self.forbidden)
        if forbidden_conflicts:
            names = ", ".join(sorted(forbidden_conflicts))
            raise ValueError(f"required/allowed and forbidden tools conflict: {names}")
        return self


class ToolFixture(_StrictModel):
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class StagingContract(_StrictModel):
    resource_profile: Literal[
        "deep_research_workflow", "amap_readonly", "test_calendar"
    ]
    cleanup: Literal["required", "skipped"]


class ReleaseScenario(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    phase: Literal["decision", "staging"]
    capability: str = Field(min_length=1)
    risk: Literal["critical", "high", "medium", "low"]
    request: str = Field(min_length=1)
    assistant_mode: Literal["standard", "deep_research"] = "standard"
    repetitions: Literal[1, 2] = 1
    tool_contract: ToolContract
    fixtures: dict[str, tuple[ToolFixture, ...]] = Field(default_factory=dict)
    state_assertions: tuple[StateAssertion, ...] = ()
    staging: StagingContract | None = None
    @model_validator(mode="after")
    def _validate_phase_contract(self) -> Self:
        if self.phase == "decision":
            missing = sorted(set(self.tool_contract.required) - set(self.fixtures))
            if missing:
                raise ValueError(f"missing fixtures for required tools: {', '.join(missing)}")
            empty = sorted(name for name, fixtures in self.fixtures.items() if not fixtures)
            if empty:
                raise ValueError(f"fixtures must not be empty: {', '.join(empty)}")
            if self.staging is not None:
                raise ValueError("decision scenario must not define staging")
            if self.risk == "critical" and self.repetitions != 2:
                raise ValueError("critical decision scenario repetitions must be 2")
        else:
            if self.staging is None:
                raise ValueError("staging scenario requires resource_profile and cleanup")
            if self.fixtures:
                raise ValueError("staging scenario must not define fixtures")
        return self
