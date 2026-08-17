"""Run and session contracts for progressively exposed capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID


CapabilityGrantSource = Literal["skill", "context", "tool_search"]
_CAPABILITY_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class CapabilityGrant(BaseModel, ABC):
    """Trusted grant that can add eligible ToolSpecs to a run catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: CapabilityGrantSource
    grant_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(default=DEFAULT_AGENT_ID, min_length=1)
    tool_names: list[str] = Field(default_factory=list)

    @property
    @abstractmethod
    def capability_id(self) -> str:
        """Return the typed Skill or Toolset subject identifier."""

    @model_validator(mode="after")
    def validate_grant(self) -> "CapabilityGrant":
        normalized = [name.strip() for name in self.tool_names]
        if any(not name for name in normalized):
            raise ValueError("tool_names must not contain blank names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("tool_names must not contain duplicates")
        object.__setattr__(self, "tool_names", normalized)
        return self


class SkillGrant(CapabilityGrant):
    """Session grant produced by loading procedural Skill guidance."""

    source: Literal["skill"] = "skill"
    skill_id: str = Field(pattern=_CAPABILITY_ID_PATTERN)

    @property
    def capability_id(self) -> str:
        return self.skill_id


class ContextToolsetGrant(CapabilityGrant):
    """Toolset grant activated from trusted structured runtime facts."""

    source: Literal["context"] = "context"
    toolset_id: str = Field(pattern=_CAPABILITY_ID_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_skill_id(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        legacy_skill_id = migrated.pop("skill_id", None)
        toolset_id = migrated.get("toolset_id")
        if (
            legacy_skill_id is not None
            and toolset_id is not None
            and legacy_skill_id != toolset_id
        ):
            raise ValueError("conflicting context Toolset subjects")
        if "toolset_id" not in migrated and legacy_skill_id is not None:
            migrated["toolset_id"] = legacy_skill_id
        return migrated

    @property
    def capability_id(self) -> str:
        return self.toolset_id


class DeferredToolsetGrant(CapabilityGrant):
    """Reserved Toolset grant produced by a trusted deferred Tool search."""

    source: Literal["tool_search"] = "tool_search"
    toolset_id: str = Field(pattern=_CAPABILITY_ID_PATTERN)

    @property
    def capability_id(self) -> str:
        return self.toolset_id


CapabilityGrantValue = Annotated[
    SkillGrant | ContextToolsetGrant | DeferredToolsetGrant,
    Field(discriminator="source"),
]
_CAPABILITY_GRANT_ADAPTER = TypeAdapter(CapabilityGrantValue)


def validate_capability_grant(
    grant: CapabilityGrant | dict[str, object],
) -> CapabilityGrantValue:
    """Parse persisted or runtime input into its concrete grant type."""

    if isinstance(grant, CapabilityGrant):
        grant = grant.model_dump(mode="python")
    return _CAPABILITY_GRANT_ADAPTER.validate_python(grant)
