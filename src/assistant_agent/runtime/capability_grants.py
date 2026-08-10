"""Run and session contracts for progressively exposed capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID


CapabilityGrantSource = Literal["skill", "context", "tool_search"]


class CapabilityGrant(BaseModel):
    """Trusted grant that can add eligible ToolSpecs to a run catalog."""

    source: CapabilityGrantSource
    grant_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(default=DEFAULT_AGENT_ID, min_length=1)
    skill_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    tool_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_grant(self) -> "CapabilityGrant":
        normalized = [name.strip() for name in self.tool_names]
        if any(not name for name in normalized):
            raise ValueError("tool_names must not contain blank names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("tool_names must not contain duplicates")
        if self.source in {"skill", "context"} and self.skill_id is None:
            raise ValueError("skill and context grants require skill_id")
        self.tool_names = normalized
        return self


class _CapabilitySessionStore(Protocol):
    def get(self, user_id: str, session_id: str) -> Any: ...

    def grant_capability(
        self,
        *,
        user_id: str,
        session_id: str,
        grant: CapabilityGrant,
    ) -> Any: ...


class CapabilityGrantController:
    """Restore and persist trusted Skill-backed grants for assistant runs."""

    def __init__(
        self,
        *,
        session_store: _CapabilitySessionStore,
        skill_root: Path,
    ) -> None:
        self.session_store = session_store
        self.skill_root = Path(skill_root)

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
            skill_id = stored.skill_id
            descriptor = descriptors.get(skill_id) if skill_id is not None else None
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
        self.session_store.grant_capability(
            user_id=state.user_id,
            session_id=state.session_id,
            grant=grant,
        )


def _grant_source_for_descriptor(descriptor: Any) -> CapabilityGrantSource | None:
    if descriptor is None:
        return None
    return "context" if descriptor.activation == "context" else "skill"


def _grant_for_descriptor(
    descriptor: Any,
    *,
    agent_id: str,
) -> CapabilityGrant:
    source = _grant_source_for_descriptor(descriptor)
    if source is None:
        raise ValueError("Cannot grant an unavailable Skill descriptor")
    return CapabilityGrant(
        source=source,
        grant_id=f"{source}:{descriptor.name}",
        agent_id=agent_id,
        skill_id=descriptor.name,
        tool_names=list(descriptor.governed_tools),
    )
