"""Structured execution profiles for reusable assistant graph instances."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.tools.models import ToolCategory, ToolSpec
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)

if TYPE_CHECKING:
    from assistant_agent.runtime.assistant_graph_state import AssistantTurnState


AssistantGraphProfileName = Literal["standard", "planner", "worker", "verifier"]
_CONTROL_TOOL_NAMES = frozenset(
    {LOAD_SKILL_TOOL_NAME, LOAD_SKILL_REFERENCE_TOOL_NAME}
)
_PROFILE_SCOPE_PREFIX = "graph_profile_scope_sha256:"
_EXECUTION_POLICY_PREFIX = "graph_execution_policy_v1_sha256:"


@dataclass(frozen=True)
class AssistantGraphProfile:
    """Trusted constraints applied to one assistant graph invocation."""

    name: AssistantGraphProfileName
    max_tool_iterations: int
    max_control_tool_iterations: int
    allowed_categories: frozenset[ToolCategory]


@dataclass(frozen=True, slots=True)
class GraphExecutionPolicy:
    """Immutable invocation limits injected through LangGraph Runtime context."""

    profile: AssistantGraphProfileName
    model_call_limit: int
    action_tool_call_limit: int
    control_tool_call_limit: int
    policy_digest: str


class GraphExecutionPolicyMismatchError(ValueError):
    """Raised when a Runtime policy does not match the checkpoint digest."""

    code = "graph_execution_policy_mismatch"


class GraphProfileMismatchError(ValueError):
    """Raised when an invocation attempts to change a checkpoint's profile."""

    code = "graph_profile_mismatch"


class GraphProfilePolicyError(ValueError):
    """Raised when checkpoint capability facts exceed the selected profile."""

    code = "graph_profile_policy_invalid"


class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileInvocationInput(_ProfileModel):
    """Trusted parent assignment projected into an assistant child graph."""

    profile: AssistantGraphProfileName
    assignment_ref: str = Field(min_length=1, max_length=1_024)
    objective: str = Field(min_length=1, max_length=10_000)
    request_text: str | None = Field(default=None, min_length=1, max_length=32_000)
    constraints: tuple[str, ...] = Field(default=(), max_length=64)
    capability_refs: tuple[str, ...] = Field(default=(), max_length=64)
    explicit_tool_allowlist: tuple[str, ...] | None = Field(
        default=None,
        max_length=256,
    )


class ProfileResponse(_ProfileModel):
    message: str = Field(max_length=32_000)
    followup_question: str | None = Field(default=None, max_length=4_000)
    output_refs: tuple[str, ...] = Field(default=(), max_length=64)


class ProfileToolTrajectoryItem(_ProfileModel):
    tool_name: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed", "rejected"]
    summary: str = Field(max_length=2_000)
    provider_call_id: str | None = Field(default=None, max_length=256)
    output_ref: str | None = Field(default=None, max_length=1_024)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=32)


class ProfileInvocationResult(_ProfileModel):
    """Bounded child result returned to a parent graph."""

    profile: AssistantGraphProfileName
    status: Literal[
        "created", "running", "waiting_user", "completed", "failed", "cancelled"
    ]
    response: ProfileResponse | None = None
    tool_trajectory: tuple[ProfileToolTrajectoryItem, ...] = Field(
        default=(),
        max_length=64,
    )
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)


ASSISTANT_GRAPH_PROFILES: Mapping[
    AssistantGraphProfileName, AssistantGraphProfile
] = MappingProxyType(
    {
        "standard": AssistantGraphProfile(
            name="standard",
            max_tool_iterations=8,
            max_control_tool_iterations=3,
            allowed_categories=frozenset({"read", "generate", "write", "dangerous"}),
        ),
        "planner": AssistantGraphProfile(
            name="planner",
            max_tool_iterations=0,
            max_control_tool_iterations=0,
            allowed_categories=frozenset(),
        ),
        "worker": AssistantGraphProfile(
            name="worker",
            max_tool_iterations=5,
            max_control_tool_iterations=0,
            allowed_categories=frozenset({"read", "generate"}),
        ),
        "verifier": AssistantGraphProfile(
            name="verifier",
            max_tool_iterations=3,
            max_control_tool_iterations=0,
            allowed_categories=frozenset({"read"}),
        ),
    }
)


def graph_execution_policy(
    *,
    profile: AssistantGraphProfileName,
    model_call_limit: int,
    action_tool_call_limit: int,
    control_tool_call_limit: int,
) -> GraphExecutionPolicy:
    """Build one validated policy and its deterministic checkpoint digest."""

    if model_call_limit < 1:
        raise ValueError("model_call_limit must be positive")
    if action_tool_call_limit < 0 or control_tool_call_limit < 0:
        raise ValueError("Tool call limits must be non-negative")
    assistant_graph_profile(profile)
    payload = json.dumps(
        {
            "schema_version": 1,
            "profile": profile,
            "model_call_limit": model_call_limit,
            "action_tool_call_limit": action_tool_call_limit,
            "control_tool_call_limit": control_tool_call_limit,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = f"{_EXECUTION_POLICY_PREFIX}{hashlib.sha256(payload).hexdigest()}"
    return GraphExecutionPolicy(
        profile=profile,
        model_call_limit=model_call_limit,
        action_tool_call_limit=action_tool_call_limit,
        control_tool_call_limit=control_tool_call_limit,
        policy_digest=digest,
    )


def validate_graph_execution_policy(
    policy: GraphExecutionPolicy,
) -> GraphExecutionPolicy:
    """Reject manually constructed policies whose digest does not bind the fields."""

    expected = graph_execution_policy(
        profile=policy.profile,
        model_call_limit=policy.model_call_limit,
        action_tool_call_limit=policy.action_tool_call_limit,
        control_tool_call_limit=policy.control_tool_call_limit,
    )
    if policy.policy_digest != expected.policy_digest:
        raise GraphExecutionPolicyMismatchError(
            "execution policy digest does not match its fields"
        )
    return policy


def default_graph_execution_policy(
    profile: AssistantGraphProfileName = "standard",
) -> GraphExecutionPolicy:
    """Return canonical profile limits for callers without a narrower slice."""

    canonical = assistant_graph_profile(profile)
    return graph_execution_policy(
        profile=canonical.name,
        model_call_limit=max(1, canonical.max_tool_iterations),
        action_tool_call_limit=canonical.max_tool_iterations,
        control_tool_call_limit=canonical.max_control_tool_iterations,
    )


def profile_execution_policy(
    profile: AssistantGraphProfileName,
    *,
    model_call_limit: int | None = None,
    tool_call_limit: int | None = None,
) -> GraphExecutionPolicy:
    """Apply an optional durable slice to canonical profile maxima."""

    if model_call_limit is not None and model_call_limit < 1:
        raise ValueError("profile model_call_limit must be positive")
    if tool_call_limit is not None and tool_call_limit < 0:
        raise ValueError("profile tool_call_limit must be non-negative")
    canonical = assistant_graph_profile(profile)
    effective_model_limit = min(
        max(1, canonical.max_tool_iterations),
        (
            model_call_limit
            if model_call_limit is not None
            else max(1, canonical.max_tool_iterations)
        ),
    )
    effective_tool_limit = min(
        canonical.max_tool_iterations,
        (
            tool_call_limit
            if tool_call_limit is not None
            else canonical.max_tool_iterations
        ),
    )
    return graph_execution_policy(
        profile=canonical.name,
        model_call_limit=effective_model_limit,
        action_tool_call_limit=effective_tool_limit,
        control_tool_call_limit=min(
            canonical.max_control_tool_iterations,
            effective_tool_limit,
        ),
    )


def assistant_graph_profile(
    profile: AssistantGraphProfileName | AssistantGraphProfile,
) -> AssistantGraphProfile:
    """Resolve a trusted profile name to its canonical immutable definition."""

    if isinstance(profile, AssistantGraphProfile):
        canonical = ASSISTANT_GRAPH_PROFILES.get(profile.name)
        if canonical != profile:
            raise ValueError("assistant graph profile is not canonical")
        return canonical
    try:
        return ASSISTANT_GRAPH_PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown assistant graph profile: {profile}") from exc


def profile_input_adapter(
    parent_state: Mapping[str, object],
    assignment: ProfileInvocationInput | Mapping[str, object],
    *,
    model_call_limit: int | None = None,
    tool_call_limit: int | None = None,
) -> "AssistantTurnState":
    """Project only admitted identity and assignment facts into child state."""

    from assistant_agent.runtime.assistant_graph_state import (
        AssistantTurnState,
        assistant_turn_state_from_request,
        validate_assistant_turn_state,
    )
    from assistant_agent.runtime.requests import RuntimeTaskUpdate, UserRequest

    invocation = (
        assignment
        if isinstance(assignment, ProfileInvocationInput)
        else ProfileInvocationInput.model_validate(assignment)
    )
    profile = assistant_graph_profile(invocation.profile)
    execution_policy = profile_execution_policy(
        profile.name,
        model_call_limit=model_call_limit,
        tool_call_limit=tool_call_limit,
    )
    specs = _registered_specs(parent_state)
    parent_available = _parent_available_tool_names(parent_state)
    explicit = (
        None
        if invocation.explicit_tool_allowlist is None
        else set(invocation.explicit_tool_allowlist)
    )
    allowed_names = tuple(
        spec.name
        for spec in specs
        if spec.name in parent_available
        and spec.category in profile.allowed_categories
        and (
            profile.max_control_tool_iterations > 0
            or spec.name not in _CONTROL_TOOL_NAMES
        )
        and (explicit is None or spec.name in explicit)
    )
    excluded_codes: list[str] = []
    for spec in specs:
        reason = None
        if spec.name not in parent_available:
            reason = "parent_catalog_not_allowed"
        elif spec.category not in profile.allowed_categories:
            reason = "profile_category_not_allowed"
        elif (
            profile.max_control_tool_iterations == 0
            and spec.name in _CONTROL_TOOL_NAMES
        ):
            reason = "profile_control_tool_not_allowed"
        elif explicit is not None and spec.name not in explicit:
            reason = "profile_explicit_not_allowed"
        if reason is not None:
            excluded_codes.append(f"{spec.name}:{reason}")

    request = UserRequest(
        user_id=_parent_text(parent_state, "user_id"),
        session_id=_parent_text(parent_state, "session_id"),
        text=invocation.request_text or invocation.objective,
        task_execution_mode="foreground",
        response_style="structured",
        runtime_task_update=RuntimeTaskUpdate(
            action="continue",
            objective=invocation.objective,
            constraints=list(invocation.constraints),
        ),
    )
    child = dict(
        assistant_turn_state_from_request(
            request,
            run_id=_parent_text(parent_state, "run_id"),
            trace_id=_parent_text(parent_state, "trace_id"),
            agent_id=_parent_text(parent_state, "agent_id"),
            profile=profile.name,
            execution_policy=execution_policy,
        )
    )
    child_request = cast(dict[str, Any], child["request"])
    child_request["capability_refs"] = list(invocation.capability_refs)
    child["capability_refs"] = list(invocation.capability_refs)
    child["context_refs"] = [
        {
            "kind": "context_section",
            "ref": invocation.assignment_ref,
            "source": "profile_assignment",
            "version": None,
            "status_code": None,
        }
    ]
    child["run_phase"] = "act"
    child["catalog"] = {
        "schema_version": "run_tool_catalog_v1",
        "available_tool_names": list(allowed_names),
        "selection_reason_codes": [
            f"graph_profile:{profile.name}",
            _profile_scope_reason(profile.name, allowed_names),
        ],
        "exclusion_reason_codes": excluded_codes,
    }
    return cast(AssistantTurnState, validate_assistant_turn_state(child))


def profile_output_adapter(
    child_state: Mapping[str, object],
) -> ProfileInvocationResult:
    """Project a child checkpoint into the bounded parent result contract."""

    from assistant_agent.runtime.assistant_graph_state import (
        validate_assistant_turn_state,
    )

    child = validate_assistant_turn_state(child_state)
    run = cast(Mapping[str, Any], child["run"])
    response_value = child.get("final_response")
    response = (
        ProfileResponse(
            message=str(response_value["message"]),
            followup_question=cast(
                str | None,
                response_value.get("followup_question"),
            ),
            output_refs=tuple(response_value.get("output_refs") or ()),
        )
        if isinstance(response_value, Mapping)
        else None
    )
    observations = cast(list[Mapping[str, Any]], child["tool_observations"])
    trajectory = tuple(
        ProfileToolTrajectoryItem(
            tool_name=str(item["tool_name"]),
            status=cast(Any, item["status"]),
            summary=str(item["summary"]),
            provider_call_id=cast(str | None, item.get("provider_call_id")),
            output_ref=cast(str | None, item.get("output_ref")),
            artifact_refs=tuple(item.get("artifact_refs") or ()),
        )
        for item in observations
    )
    artifact_refs: list[str] = []
    if response is not None:
        artifact_refs.extend(response.output_refs)
    for item in trajectory:
        artifact_refs.extend(item.artifact_refs)
    for item in cast(list[Mapping[str, Any]], child["outputs_by_step"]):
        artifact_refs.extend(cast(list[str], item.get("artifact_refs") or []))
    return ProfileInvocationResult(
        profile=cast(AssistantGraphProfileName, child["profile"]),
        status=cast(Any, run["status"]),
        response=response,
        tool_trajectory=trajectory,
        artifact_refs=tuple(dict.fromkeys(artifact_refs)),
    )


def resolve_resume_profile(
    checkpoint_state: Mapping[str, object],
    requested_profile: AssistantGraphProfileName | AssistantGraphProfile | None = None,
) -> AssistantGraphProfile:
    """Inherit a checkpoint profile, rejecting every attempted profile switch."""

    from assistant_agent.runtime.assistant_graph_state import (
        validate_assistant_turn_state,
    )

    checkpoint = validate_assistant_turn_state(checkpoint_state)
    persisted = assistant_graph_profile(
        cast(AssistantGraphProfileName, checkpoint["profile"])
    )
    if requested_profile is None:
        return persisted
    requested = assistant_graph_profile(requested_profile)
    if requested.name != persisted.name:
        raise GraphProfileMismatchError(
            f"checkpoint profile {persisted.name!r} cannot resume as {requested.name!r}"
        )
    return persisted


def _registered_specs(parent_state: Mapping[str, object]) -> tuple["ToolSpec", ...]:
    raw = parent_state.get("registered_tool_specs")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("parent state must provide registered_tool_specs")
    return tuple(
        item if isinstance(item, ToolSpec) else ToolSpec.model_validate(item)
        for item in raw
    )


def _parent_available_tool_names(parent_state: Mapping[str, object]) -> set[str]:
    raw = parent_state.get("available_tool_names")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError("parent state must provide available_tool_names")
    if any(not isinstance(item, str) or not item for item in raw):
        raise ValueError("parent available_tool_names must contain non-empty strings")
    return set(cast(Any, raw))


def _parent_text(parent_state: Mapping[str, object], field: str) -> str:
    value = parent_state.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"parent state must provide non-empty {field}")
    return value


def profile_scope_matches(
    profile: AssistantGraphProfileName,
    available_tool_names: tuple[str, ...] | list[str],
    reason_codes: tuple[str, ...] | list[str],
) -> bool:
    """Validate that a persisted Tool scope still matches its adapter output."""

    expected = _profile_scope_reason(profile, available_tool_names)
    scope_reasons = [
        item for item in reason_codes if item.startswith(_PROFILE_SCOPE_PREFIX)
    ]
    return scope_reasons == [expected]


def _profile_scope_reason(
    profile: AssistantGraphProfileName,
    available_tool_names: tuple[str, ...] | list[str],
) -> str:
    payload = json.dumps(
        [profile, sorted(available_tool_names)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return f"{_PROFILE_SCOPE_PREFIX}{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ASSISTANT_GRAPH_PROFILES",
    "AssistantGraphProfile",
    "AssistantGraphProfileName",
    "GraphExecutionPolicy",
    "GraphExecutionPolicyMismatchError",
    "GraphProfileMismatchError",
    "GraphProfilePolicyError",
    "ProfileInvocationInput",
    "ProfileInvocationResult",
    "assistant_graph_profile",
    "default_graph_execution_policy",
    "graph_execution_policy",
    "profile_input_adapter",
    "profile_execution_policy",
    "profile_output_adapter",
    "profile_scope_matches",
    "resolve_resume_profile",
    "validate_graph_execution_policy",
]
