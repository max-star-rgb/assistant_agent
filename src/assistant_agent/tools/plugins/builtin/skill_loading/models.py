"""Contracts for governed project Skill loading."""

from pydantic import BaseModel, Field


class LoadSkillRequest(BaseModel):
    skill_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="已注册的项目 Skill 标识。",
    )


class LoadSkillReferenceRequest(LoadSkillRequest):
    reference_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="load_skill 返回的已注册 reference 标识。",
    )


class LoadSkillResult(BaseModel):
    status: str
    skill_id: str
    content: str
    reference_ids: list[str] = Field(default_factory=list)


class LoadSkillReferenceResult(BaseModel):
    status: str
    skill_id: str
    reference_id: str
    content: str
