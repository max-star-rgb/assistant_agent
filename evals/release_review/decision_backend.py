from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.registry import ToolRegistry

from .contracts import ReleaseScenario


class ScenarioBackendCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    input: dict[str, Any]
    call_index: int = Field(ge=1)
    status: Literal["succeeded", "failed"]


class ScenarioExecutionBackend:
    """Return deterministic fixture results inside ToolExecutor governance."""

    def __init__(self, scenario: ReleaseScenario) -> None:
        if scenario.phase != "decision":
            raise ValueError("ScenarioExecutionBackend requires a decision scenario")
        self.scenario = scenario.model_copy(deep=True)
        self.calls: list[ScenarioBackendCall] = []
        self._fixture_offsets: dict[str, int] = defaultdict(int)

    def run(
        self,
        registry: ToolRegistry,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del registry, context
        normalized_input = (
            tool_input.model_dump(mode="python")
            if isinstance(tool_input, BaseModel)
            else deepcopy(tool_input)
        )
        fixtures = self.scenario.fixtures.get(tool_name, ())
        offset = self._fixture_offsets[tool_name]
        self._fixture_offsets[tool_name] += 1
        if offset >= len(fixtures):
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                error=(
                    "release_fixture_missing: "
                    f"scenario {self.scenario.id} has no fixture for "
                    f"{tool_name} call {offset + 1}"
                ),
            )
        else:
            fixture = fixtures[offset].model_copy(deep=True)
            result = ToolResult(
                tool_name=tool_name,
                success=fixture.success,
                data=deepcopy(fixture.data),
                error=fixture.error,
            )
        self.calls.append(
            ScenarioBackendCall(
                tool_name=tool_name,
                input=normalized_input,
                call_index=len(self.calls) + 1,
                status="succeeded" if result.success else "failed",
            )
        )
        return result

