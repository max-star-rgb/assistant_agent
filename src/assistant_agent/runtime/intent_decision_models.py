"""Unified intent decision schema for router candidates."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from assistant_agent.runtime.capability_models import CapabilityName, contract_for_intent


DecisionSource = Literal["rule", "llm", "mock_llm", "hybrid", "fallback"]


class PlanStep(BaseModel):
    """One candidate planning step produced by an intent router."""

    step_id: str = Field(min_length=1)
    capability: CapabilityName
    tool_name: str | None = None
    reason: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    optional: bool = False

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str | None, info: object) -> str | None:
        data = getattr(info, "data", {})
        capability = data.get("capability")
        if capability is None:
            return value
        expected_tool = contract_for_intent(capability).tool_name
        if value is not None and value != expected_tool:
            raise ValueError(f"tool_name must be {expected_tool!r} for capability {capability!r}")
        return value


class IntentDecision(BaseModel):
    """Structured output produced by rule, mock, or optional LLM intent routers."""

    primary_intent: CapabilityName
    capabilities: list[CapabilityName] = Field(default_factory=list)
    plan_steps: list[PlanStep] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: DecisionSource = "rule"
    reason: str = ""
    matched_rules: list[str] = Field(default_factory=list)
    raw_output_ref: str | None = None

    @field_validator("capabilities")
    @classmethod
    def ensure_unique_capabilities(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        deduped: list[CapabilityName] = []
        for capability in value:
            if capability not in deduped:
                deduped.append(capability)
        return deduped
