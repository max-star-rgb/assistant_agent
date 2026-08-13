"""Safe product contracts for native graph time-travel capabilities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GraphCheckpointSelector(_StrictModel):
    """Opaque reference to one re-entry-safe native graph checkpoint."""

    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")


class GraphCheckpointSummary(_StrictModel):
    """Bounded product-safe projection of one native state snapshot."""

    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")
    created_at: datetime
    status: Literal["running", "waiting_user", "completed", "failed", "cancelled"]
    next_nodes: tuple[str, ...]
    has_interrupt: bool
    graph_version: str
    state_schema_version: int


class GraphReplayRequest(_StrictModel):
    """Request to replay one owner-bound, re-entry-safe checkpoint."""

    selector: GraphCheckpointSelector


class GraphForkPatch(_StrictModel):
    """Allowlisted product fields that may differ on one native branch."""

    request_text: str | None = Field(default=None, max_length=32_000)
    response_style: Literal["conversation", "concise", "structured", "voice"] | None = (
        None
    )


class GraphForkRequest(_StrictModel):
    """Request one owner-bound branch without exposing native checkpoint IDs."""

    selector: GraphCheckpointSelector
    patch: GraphForkPatch
    refresh_memory: bool = False


TimeTravelEffectDecision = Literal[
    "safe", "barrier_required", "outcome_unknown", "forbidden"
]


class _ToolRegistry(Protocol):
    def get_spec(self, name: str) -> Any: ...


class _OperationStore(Protocol):
    def load(self, operation_key: str) -> Any | None: ...


class TimeTravelEffectPolicy:
    """Classify only effects reachable from the selected checkpoint."""

    def __init__(
        self,
        *,
        registry: _ToolRegistry,
        operation_store: _OperationStore,
        runtime_state: Any | None = None,
        context_metadata: Mapping[str, Any] | None = None,
    ):
        self._registry = registry
        self._operation_store = operation_store
        self._runtime_state = runtime_state
        self._context_metadata = dict(context_metadata or {})

    def classify(
        self,
        state: Mapping[str, Any],
        next_nodes: tuple[str, ...],
    ) -> TimeTravelEffectDecision:
        pending = state.get("pending_tool_calls")
        if not pending:
            return "safe"
        continuation = state.get("continuation")
        if continuation not in {"execute_tool", "await_input"}:
            return "forbidden"
        if next_nodes not in {("prepare_invocation",), ("await_input",)}:
            return "forbidden"
        if not isinstance(pending, (list, tuple)) or not pending:
            return "forbidden"

        decisions: list[TimeTravelEffectDecision] = []
        catalog = state.get("catalog")
        runtime_generation = getattr(self._registry, "generation", None)
        if (
            not isinstance(catalog, Mapping)
            or catalog.get("registry_generation") != runtime_generation
        ):
            return "forbidden"
        for ordinal, value in enumerate(pending):
            if not isinstance(value, Mapping):
                return "forbidden"
            tool_name = value.get("tool_name")
            operation_scope_id = value.get("operation_scope_id")
            if not isinstance(tool_name, str) or not isinstance(
                operation_scope_id, str
            ):
                return "forbidden"
            if (
                not operation_scope_id.startswith("toolop:")
                or len(operation_scope_id) != 71
            ):
                return "forbidden"
            arguments = value.get("arguments")
            if not isinstance(arguments, (list, tuple)):
                return "forbidden"
            try:
                pending_input = {
                    str(argument["name"]): json.loads(str(argument["value_json"]))
                    for argument in arguments
                    if isinstance(argument, Mapping)
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return "forbidden"
            if len(pending_input) != len(arguments):
                return "forbidden"
            try:
                tool_spec = self._registry.get_spec(tool_name)
                category = tool_spec.category
            except Exception:
                return "forbidden"
            checkpoint_category = value.get("effect_category")
            if checkpoint_category not in {"read", "write", "dangerous"}:
                return "forbidden"
            if category != checkpoint_category:
                return "forbidden"
            from assistant_agent.runtime.tool_operation_barrier import (
                tool_contract_digest,
                tool_execution_contract_digest,
            )

            if value.get("tool_contract_digest") != tool_contract_digest(tool_spec):
                return "forbidden"
            if value.get("execution_contract_digest") != tool_execution_contract_digest(
                self._registry.get(tool_name),
                tool_spec,
            ):
                return "forbidden"
            if category not in {"read", "write", "dangerous"}:
                return "forbidden"
            from assistant_agent.runtime.tool_operation_barrier import (
                normalized_tool_input_digest,
                stable_assistant_thread_id,
                stable_operation_scope_id,
                tool_operation_key,
            )

            request = state.get("request")
            run = state.get("run")
            if not isinstance(request, Mapping) or not isinstance(run, Mapping):
                return "forbidden"
            try:
                thread_id = stable_assistant_thread_id(
                    agent_id=str(run["agent_id"]),
                    user_id=str(request["user_id"]),
                    session_id=str(request["session_id"]),
                )
                expected_scope = stable_operation_scope_id(
                    thread_id=thread_id,
                    turn_origin_id=str(state["turn_origin_id"]),
                    assistant_iteration=int(state["assistant_iterations"]),
                    call_ordinal=ordinal,
                    tool_name=tool_name,
                    normalized_input_digest=normalized_tool_input_digest(pending_input),
                )
                operation_key = tool_operation_key(
                    thread_id=thread_id,
                    operation_scope_id=operation_scope_id,
                    profile=str(state["profile"]),
                    tool_name=tool_name,
                )
            except (KeyError, TypeError, ValueError):
                return "forbidden"
            if operation_scope_id != expected_scope:
                return "forbidden"
            record = self._operation_store.load(operation_key)
            if record is not None and (
                getattr(record, "operation_key", None) != operation_key
                or getattr(record, "thread_id", None) != thread_id
                or getattr(record, "operation_scope_id", None) != operation_scope_id
                or getattr(record, "profile", None) != state["profile"]
                or getattr(record, "tool_name", None) != tool_name
            ):
                return "forbidden"
            if category == "read":
                # A historical side-effecting implementation may have left a
                # ledger row before the current trusted contract became read.
                if record is not None:
                    return "forbidden"
                decisions.append("safe")
                continue
            expected_input_digest = self._bound_input_digest(
                tool_name=tool_name,
                pending_input=pending_input,
                operation_key=operation_key,
                step_ordinal=ordinal,
                prior_observation_count=len(state.get("tool_observations") or ()),
            )
            if (
                expected_input_digest is None
                or value.get("bound_input_digest") != expected_input_digest
                or (
                    record is not None
                    and getattr(record, "input_digest", None) != expected_input_digest
                )
            ):
                return "forbidden"
            if record is not None and getattr(record, "status", None) in {
                "reserved",
                "invoking",
                "outcome_unknown",
            }:
                decisions.append("outcome_unknown")
            else:
                decisions.append("barrier_required")
        if "outcome_unknown" in decisions:
            return "outcome_unknown"
        if "barrier_required" in decisions:
            return "barrier_required"
        return "safe"

    def _bound_input_digest(
        self,
        *,
        tool_name: str,
        pending_input: dict[str, Any],
        operation_key: str,
        step_ordinal: int,
        prior_observation_count: int,
    ) -> str | None:
        if self._runtime_state is None:
            return None
        try:
            from assistant_agent.tools.input_binding import (
                bind_runtime_tool_input,
                runtime_bound_input_fields,
            )
            from assistant_agent.runtime.tool_operation_barrier import (
                normalized_tool_input_digest,
            )

            tool = self._registry.get(tool_name)
            bound = bind_runtime_tool_input(
                tool,
                pending_input,
                state=self._runtime_state,
                step_id=f"assistant_loop_{prior_observation_count + step_ordinal + 1}",
                context_metadata=self._context_metadata,
            )
            if "idempotency_key" in runtime_bound_input_fields(tool) and not bound.get(
                "idempotency_key"
            ):
                bound["idempotency_key"] = operation_key
            validated = tool.input_schema.model_validate(bound)
            return normalized_tool_input_digest(validated.model_dump(mode="json"))
        except Exception:
            return None


def fork_patch_preserves_pending_effects(
    state: Mapping[str, Any],
    patch: GraphForkPatch,
) -> bool:
    """Return false when any pending call could consume patched request facts."""

    return not (state.get("pending_tool_calls") and patch.model_fields_set)


def fork_patch_for_assistant_state(
    historical: Mapping[str, Any],
    patch: GraphForkPatch,
) -> dict[str, Any]:
    """Apply only product-owned request fields to validated checkpoint state."""

    if not isinstance(patch, GraphForkPatch):
        raise TypeError("patch must be a GraphForkPatch")
    from assistant_agent.runtime.assistant_graph_state import (
        validate_assistant_turn_state,
    )

    persisted = validate_assistant_turn_state(historical)
    updated = deepcopy(dict(persisted))
    request = dict(cast(Mapping[str, Any], persisted["request"]))
    if "request_text" in patch.model_fields_set:
        prior_text = request.get("text")
        messages = list(request.get("messages") or ())
        if (
            prior_text is not None
            and messages
            and messages[-1].get("role") == "user"
            and messages[-1].get("text") == prior_text
        ):
            messages.pop()
        request["text"] = patch.request_text
        if patch.request_text is not None:
            messages.append(
                {"role": "user", "text": patch.request_text, "tool_call_id": None}
            )
        request["messages"] = messages[-128:]
    if "response_style" in patch.model_fields_set and patch.response_style is not None:
        request["response_style"] = patch.response_style
    updated["request"] = request
    return cast(dict[str, Any], validate_assistant_turn_state(updated))


def graph_history_ref(
    *,
    thread_id: str,
    snapshot_config: Mapping[str, Any],
) -> str:
    """Derive an opaque selector without retaining a process-local lookup map."""

    canonical = json.dumps(
        {
            "domain": "assistant_graph_history_ref_v1",
            "thread_id": thread_id,
            "config": snapshot_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "ghr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "GraphCheckpointSelector",
    "GraphCheckpointSummary",
    "GraphForkPatch",
    "GraphForkRequest",
    "GraphReplayRequest",
    "TimeTravelEffectPolicy",
    "fork_patch_preserves_pending_effects",
    "fork_patch_for_assistant_state",
    "graph_history_ref",
]
