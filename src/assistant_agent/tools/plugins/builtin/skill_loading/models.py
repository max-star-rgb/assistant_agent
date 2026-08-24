"""Contracts for governed project Skill loading."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoadSkillRequest(BaseModel):
    skill_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="已注册的内部工作流标识。",
    )


class LoadSkillReferenceRequest(LoadSkillRequest):
    reference_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="load_skill 返回的已注册 reference 标识。",
    )


class SkillCapabilityActivation(BaseModel):
    """Phase-neutral capability activation produced by one loaded Skill."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    projection: Literal["phase_aware"] = "phase_aware"
    tool_names: tuple[str, ...] = Field(default=(), max_length=128)


class LoadSkillResult(BaseModel):
    status: str
    skill_id: str
    content: str
    reference_ids: list[str] = Field(default_factory=list)
    capability_activation: SkillCapabilityActivation
    # Compatibility-only artifact field. Model-facing observations use the
    # explicit phase-aware activation contract above.
    granted_tools: list[str] = Field(default_factory=list)
    unavailable_tools: list[str] = Field(default_factory=list)


class LoadSkillReferenceResult(BaseModel):
    status: str
    skill_id: str
    reference_id: str
    content: str
