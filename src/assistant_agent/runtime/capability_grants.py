"""Run and session contracts for progressively exposed capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

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


class _CapabilitySessionStore(Protocol):
    def get(self, user_id: str, session_id: str) -> Any: ...

    def grant_capability(
        self,
        *,
        user_id: str,
        session_id: str,
        grant: CapabilityGrantValue,
    ) -> Any: ...


class CapabilityGrantController:
    """Restore and persist trusted Skill-backed grants for assistant runs."""

    def __init__(
        self,
        *,
        session_store: _CapabilitySessionStore,
        skill_root: Path,
        registered_tool_specs: list[Any] | None = None,
    ) -> None:
        self.session_store = session_store
        self.skill_root = Path(skill_root)
        self.registered_tool_specs = list(registered_tool_specs or [])

    def prepare_run(self, state: Any, tool_specs: list[Any]) -> None:
        """Restore session grants and activate structurally eligible context Skills."""

        from assistant_agent.context.tool_catalog import qualify_tool_specs
        from assistant_agent.skills.loading import load_repo_skill_descriptors

        catalog = load_repo_skill_descriptors(self.skill_root)
        descriptors = {descriptor.name: descriptor for descriptor in catalog.descriptors}
        record = self.session_store.get(state.user_id, state.session_id)
        restored_ids: list[str] = []
        for stored in getattr(record, "capability_grants", []) if record is not None else []:
            if stored.agent_id != state.agent_id:
                continue
            descriptor = descriptors.get(stored.capability_id)
            expected_source = _grant_source_for_descriptor(descriptor)
            if descriptor is None or stored.source != expected_source:
                continue
            grant = _grant_for_descriptor(
                descriptor,
                agent_id=state.agent_id,
            )
            state.upsert_capability_grant(grant)
            restored_ids.append(grant.grant_id)
        state.session_restored_grant_ids = restored_ids

        qualification = qualify_tool_specs(
            state.request,
            tool_specs,
            catalog=catalog,
        )
        eligible_tool_names = {
            spec.name for spec in qualification.qualified_tool_specs
        }
        for descriptor in catalog.descriptors:
            if descriptor.activation != "context":
                continue
            if not set(descriptor.governed_tools).intersection(eligible_tool_names):
                continue
            grant = _grant_for_descriptor(
                descriptor,
                agent_id=state.agent_id,
            )
            state.upsert_capability_grant(grant)
            self.session_store.grant_capability(
                user_id=state.user_id,
                session_id=state.session_id,
                grant=grant,
            )

    def handle_tool_result(self, state: Any, result: Any) -> None:
        """Promote a successful governed ``load_skill`` result."""

        from assistant_agent.skills.loading import load_repo_skill_descriptors
        from assistant_agent.tools.ids import LOAD_SKILL_TOOL_NAME

        if (
            result.tool_name != LOAD_SKILL_TOOL_NAME
            or not result.success
            or not isinstance(result.data, dict)
        ):
            return
        skill_id = result.data.get("skill_id")
        if not isinstance(skill_id, str):
            return
        catalog = load_repo_skill_descriptors(self.skill_root)
        descriptor = next(
            (
                item
                for item in catalog.descriptors
                if item.name == skill_id
                and item.activation == "model"
                and not item.disable_model_invocation
            ),
            None,
        )
        if descriptor is None:
            return
        grant = _grant_for_descriptor(
            descriptor,
            agent_id=state.agent_id,
        )
        state.upsert_capability_grant(grant)
        self._annotate_load_result(
            state=state,
            result=result,
            catalog=catalog,
            descriptor=descriptor,
        )
        self.session_store.grant_capability(
            user_id=state.user_id,
            session_id=state.session_id,
            grant=grant,
        )

    def _annotate_load_result(
        self,
        *,
        state: Any,
        result: Any,
        catalog: Any,
        descriptor: Any,
    ) -> None:
        """Report the governed tools actually available after this grant."""

        if not self.registered_tool_specs:
            return
        from assistant_agent.context.tool_catalog import select_prompt_tool_specs

        selection = select_prompt_tool_specs(
            state.request,
            self.registered_tool_specs,
            skill_catalog=catalog,
            capability_grants=state.capability_grants,
        )
        available = set(selection.run_tool_catalog.available_tool_names)
        granted_tools = [
            name for name in descriptor.governed_tools if name in available
        ]
        unavailable_tools = [
            name for name in descriptor.governed_tools if name not in available
        ]
        for payload in (result.data, result.model_observation, result.trace_summary):
            if not isinstance(payload, dict):
                continue
            payload["granted_tools"] = list(granted_tools)
            payload["unavailable_tools"] = list(unavailable_tools)


def _grant_source_for_descriptor(descriptor: Any) -> CapabilityGrantSource | None:
    if descriptor is None:
        return None
    return "context" if descriptor.activation == "context" else "skill"


def _grant_for_descriptor(
    descriptor: Any,
    *,
    agent_id: str,
) -> SkillGrant | ContextToolsetGrant:
    source = _grant_source_for_descriptor(descriptor)
    if source is None:
        raise ValueError("Cannot grant an unavailable Skill descriptor")
    common = {
        "grant_id": f"{source}:{descriptor.name}",
        "agent_id": agent_id,
        "tool_names": list(descriptor.governed_tools),
    }
    if source == "context":
        return ContextToolsetGrant(
            **common,
            toolset_id=descriptor.name,
        )
    return SkillGrant(
        **common,
        skill_id=descriptor.name,
    )
