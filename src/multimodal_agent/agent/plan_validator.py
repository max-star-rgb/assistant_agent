"""Boundary validation for LLM-generated plans."""

from pydantic import BaseModel, Field

from multimodal_agent.schemas.planning import TaskPlan
from multimodal_agent.tools.registry import ToolRegistry


class PlanValidationResult(BaseModel):
    """Validation result for a planner-produced TaskPlan."""

    accepted: bool
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class PlanValidator:
    """Validate plan structure without injecting business routing rules."""

    def __init__(self, *, max_steps: int = 8) -> None:
        self.max_steps = max_steps

    def validate(self, plan: TaskPlan, registry: ToolRegistry) -> PlanValidationResult:
        if plan.requires_followup:
            return PlanValidationResult(
                accepted=True,
                code="followup_required",
                message="Plan requests user follow-up before execution.",
            )
        if not plan.steps:
            return _reject("empty_plan", "Plan must include at least one step.")
        if len(plan.steps) > self.max_steps:
            return _reject("plan_too_large", f"Plan has {len(plan.steps)} steps; max is {self.max_steps}.")

        step_ids = [step.step_id for step in plan.steps]
        if len(set(step_ids)) != len(step_ids):
            return _reject("duplicate_step_id", "Plan step_id values must be unique.")

        known_steps = set(step_ids)
        for step in plan.steps:
            for dependency in step.depends_on:
                if dependency not in known_steps:
                    return _reject(
                        "unknown_dependency",
                        f"Step {step.step_id} depends on unknown step {dependency}.",
                    )
            if step.tool_name is not None and step.tool_name not in registry.list():
                return _reject(
                    "unknown_tool",
                    f"Step {step.step_id} references unknown tool {step.tool_name}.",
                )

        cycle = _first_cycle(plan)
        if cycle:
            return _reject("cyclic_dependency", f"Plan dependencies contain a cycle: {' -> '.join(cycle)}.")

        if not any(step.tool_name for step in plan.steps):
            return _reject("no_executable_steps", "Plan must include at least one executable tool step.")

        return PlanValidationResult(accepted=True, code="accepted", message="Plan accepted.")


def _first_cycle(plan: TaskPlan) -> list[str]:
    dependencies = {step.step_id: list(step.depends_on) for step in plan.steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(step_id: str) -> list[str]:
        if step_id in visited:
            return []
        if step_id in visiting:
            try:
                start = path.index(step_id)
            except ValueError:
                start = 0
            return path[start:] + [step_id]
        visiting.add(step_id)
        path.append(step_id)
        for dependency in dependencies.get(step_id, []):
            cycle = visit(dependency)
            if cycle:
                return cycle
        path.pop()
        visiting.remove(step_id)
        visited.add(step_id)
        return []

    for step_id in dependencies:
        cycle = visit(step_id)
        if cycle:
            return cycle
    return []


def _reject(code: str, message: str) -> PlanValidationResult:
    return PlanValidationResult(accepted=False, code=code, message=message)
