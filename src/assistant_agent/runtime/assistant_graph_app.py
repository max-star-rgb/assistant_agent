"""Stable compiled application for the assistant turn graph."""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterator, Literal, TypeAlias, cast

from langgraph.types import Command

from assistant_agent.runtime.assistant_loop_graph import (
    build_assistant_loop_graph,
    build_namespaced_assistant_loop_graph,
)
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    assistant_capability_ref_identity,
    persisted_request_from_user_request,
    validate_assistant_turn_state,
    validate_assistant_runtime_refs,
)
from assistant_agent.runtime.assistant_interrupts import (
    AssistantInterrupt,
    AssistantInterruptContractError,
    AssistantResume,
    validate_assistant_interrupt_request,
    validate_assistant_resume,
    validate_resume_for_interrupt,
)
from assistant_agent.runtime.assistant_graph_profiles import (
    AssistantGraphProfile,
    AssistantGraphProfileName,
    assistant_graph_profile,
)
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.runtime.graph_invocation_claims import (
    GraphInvocationClaimCapacityExceeded,
    GraphInvocationClaimConflict,
    GraphInvocationThreadActive,
    graph_invocation_owner_digest,
)
from assistant_agent.runtime.graph_time_travel import (
    GraphCheckpointSelector,
    GraphCheckpointSummary,
    GraphForkRequest,
    GraphReplayRequest,
    fork_patch_for_assistant_state,
    graph_history_ref,
)
from assistant_agent.runtime.tool_operation_barrier import stable_assistant_thread_id
from assistant_agent.runtime.product_event_projector import (
    validate_runtime_product_fact,
)
from assistant_agent.observability.langsmith_config import LangSmithConfig
from assistant_agent.observability.langsmith_native import (
    native_graph_trace_scope,
    native_langsmith_tracing,
)


_MISSING_FINAL_STATE = object()
_HISTORY_PAGE_SIZE = 100
_HISTORY_SCAN_LIMIT = 500


class GraphExecutionError(RuntimeError):
    """Structured failure raised when a graph run cannot produce a final state."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class GraphExecutionIdentity:
    """LangGraph execution identity for one assistant conversation turn."""

    thread_id: str
    run_id: str
    agent_id: str

    @classmethod
    def for_assistant_turn(
        cls,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> "GraphExecutionIdentity":
        return cls(
            thread_id=stable_assistant_thread_id(
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
            ),
            run_id=run_id,
            agent_id=agent_id,
        )

    def runnable_config(self) -> dict[str, dict[str, str]]:
        return {
            "configurable": {
                "thread_id": self.thread_id,
                "run_id": self.run_id,
            }
        }


GraphStreamMode = Literal[
    "values", "updates", "messages", "custom", "tasks", "checkpoints"
]
GraphDurability = Literal["sync", "async", "exit"]
_GRAPH_STREAM_MODES = frozenset(
    {"values", "updates", "messages", "custom", "tasks", "checkpoints"}
)
_GRAPH_DURABILITY_MODES = frozenset({"sync", "async", "exit"})


@dataclass(frozen=True)
class GraphStreamSubscription:
    """Trusted native stream selection; never serialize this onto product wire."""

    modes: tuple[GraphStreamMode, ...]
    include_subgraphs: bool = True
    durability: GraphDurability | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.modes, tuple)
            or not self.modes
            or any(mode not in _GRAPH_STREAM_MODES for mode in self.modes)
        ):
            raise ValueError("graph stream modes must use the native v2 allowlist")
        if len(self.modes) != len(set(self.modes)):
            raise ValueError("graph stream modes must be unique")
        if type(self.include_subgraphs) is not bool:
            raise ValueError("include_subgraphs must be a bool")
        if (
            self.durability is not None
            and self.durability not in _GRAPH_DURABILITY_MODES
        ):
            raise ValueError("graph stream durability must be sync, async, or exit")

    def native_kwargs(self) -> dict[str, object]:
        values: dict[str, object] = {
            "stream_mode": list(self.modes),
            "subgraphs": self.include_subgraphs,
            "version": "v2",
        }
        if self.durability is not None:
            values["durability"] = self.durability
        return values


ASSISTANT_GRAPH_STREAM_SUBSCRIPTION = GraphStreamSubscription(
    modes=("values", "updates", "messages", "custom", "tasks", "checkpoints"),
    include_subgraphs=True,
)


@dataclass(frozen=True)
class GraphStreamPart:
    """One normalized item from a LangGraph v2 execution stream."""

    type: GraphStreamMode
    namespace: tuple[str, ...]
    data: Any


class GraphValuesPart(GraphStreamPart):
    type: Literal["values"]


class GraphUpdatePart(GraphStreamPart):
    type: Literal["updates"]


class GraphMessagePart(GraphStreamPart):
    type: Literal["messages"]


class GraphCustomPart(GraphStreamPart):
    type: Literal["custom"]


class GraphTaskPart(GraphStreamPart):
    type: Literal["tasks"]


class GraphCheckpointPart(GraphStreamPart):
    type: Literal["checkpoints"]


ValidatedGraphStreamPart: TypeAlias = (
    GraphValuesPart
    | GraphUpdatePart
    | GraphMessagePart
    | GraphCustomPart
    | GraphTaskPart
    | GraphCheckpointPart
)


def parse_graph_stream_part(raw: Mapping[str, Any]) -> ValidatedGraphStreamPart:
    """Validate one LangGraph v2 envelope before any observer can consume it."""

    if not isinstance(raw, Mapping):
        raise ValueError("graph stream envelope must be a mapping")
    stream_type = raw.get("type")
    if stream_type not in _GRAPH_STREAM_MODES:
        raise ValueError("graph stream envelope has an unsupported type")
    raw_namespace = raw.get("ns", ())
    if not isinstance(raw_namespace, (tuple, list)) or any(
        not isinstance(item, str) or not item for item in raw_namespace
    ):
        raise ValueError("graph stream namespace must contain non-empty strings")
    if "data" not in raw:
        raise ValueError("graph stream envelope has no data")
    data = raw["data"]
    namespace = tuple(raw_namespace)
    part_types: dict[str, type[GraphStreamPart]] = {
        "values": GraphValuesPart,
        "updates": GraphUpdatePart,
        "messages": GraphMessagePart,
        "custom": GraphCustomPart,
        "tasks": GraphTaskPart,
        "checkpoints": GraphCheckpointPart,
    }
    if stream_type == "messages":
        if (
            not isinstance(data, (tuple, list))
            or len(data) != 2
            or data[0] is None
            or not isinstance(data[1], Mapping)
        ):
            raise ValueError("messages stream data must be a message/metadata pair")
        data = (data[0], data[1])
    elif stream_type != "custom" and not isinstance(data, Mapping):
        raise ValueError(f"{stream_type} stream data must be a mapping")
    part_type = part_types[cast(str, stream_type)]
    return cast(
        ValidatedGraphStreamPart,
        part_type(
            type=cast(GraphStreamMode, stream_type), namespace=namespace, data=data
        ),
    )


@dataclass(frozen=True)
class GraphStreamResult:
    """Authoritative graph outcome plus the native stream that produced it."""

    final_state: AssistantTurnState
    parts: tuple[GraphStreamPart, ...]
    status: Literal["completed", "interrupted"] = "completed"
    interrupts: tuple[AssistantInterrupt, ...] = ()
    checkpoint_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class _GraphInvocationClaimLease:
    begin_native: Callable[[], None]
    mark_terminal: Callable[[], None]


class AssistantTurnGraphApp:
    """Own the one compiled assistant graph shared by a runtime instance."""

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        langsmith_config: LangSmithConfig | None = None,
    ) -> None:
        self._graph = build_assistant_loop_graph(
            checkpointer=checkpointer,
            profile="standard",
            graph_name="AssistantTurnGraph",
        )
        self._profile_graphs: dict[AssistantGraphProfileName, Any] = {}
        self._langsmith_config = langsmith_config or _default_langsmith_config()

    @classmethod
    def from_compiled_graph(
        cls,
        graph: Any,
        *,
        langsmith_config: LangSmithConfig | None = None,
    ) -> "AssistantTurnGraphApp":
        """Wrap an already compiled graph without compiling another one."""

        app = cls.__new__(cls)
        app._graph = graph
        app._profile_graphs = {}
        app._langsmith_config = langsmith_config or _default_langsmith_config()
        return app

    @property
    def graph(self) -> Any:
        """Return the compiled graph without allowing replacement."""

        return self._graph

    def graph_for_profile(
        self,
        profile: AssistantGraphProfileName | AssistantGraphProfile,
    ) -> Any:
        """Return a reusable child graph that inherits its parent's saver."""

        canonical = assistant_graph_profile(profile)
        cached = self._profile_graphs.get(canonical.name)
        if cached is not None:
            return cached
        graph = build_assistant_loop_graph(
            checkpointer=None,
            profile=canonical.name,
            graph_name=f"AssistantTurnGraph.{canonical.name}",
        )
        graph.config = {
            "metadata": {"graph_profile": canonical.name},
            "tags": [
                "assistant_turn_graph",
                f"assistant_profile:{canonical.name}",
            ],
        }
        self._profile_graphs[canonical.name] = graph
        return graph

    def namespaced_graph_for_profile(
        self,
        profile: AssistantGraphProfileName | AssistantGraphProfile,
        *,
        state_schema: type,
        context_schema: type,
        child_state_key: str,
        runtime_context_resolver: Callable[[Any, Any, object], GraphRuntimeContext],
    ) -> Any:
        """Compile a native profile graph over an explicit parent-owned channel."""

        canonical = assistant_graph_profile(profile)
        graph = build_namespaced_assistant_loop_graph(
            state_schema=state_schema,
            context_schema=context_schema,
            child_state_key=child_state_key,
            runtime_context_resolver=runtime_context_resolver,
            profile=canonical.name,
            graph_name=f"AssistantTurnGraph.{canonical.name}",
        )
        graph.config = {
            "metadata": {"graph_profile": canonical.name},
            "tags": [
                "assistant_turn_graph",
                f"assistant_profile:{canonical.name}",
            ],
        }
        return graph

    def invoke(
        self,
        input_state: AssistantTurnState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> AssistantTurnState:
        """Invoke the compiled graph inside the same native tracing context."""

        with self._invocation_claim_scope(
            identity=identity,
            context=context,
        ) as claim_lease:
            with self._native_tracing(identity):
                with native_graph_trace_scope() as callbacks:
                    config = self._runnable_config(identity, callbacks=callbacks)
                    claim_lease.begin_native()
                    final_state = cast(
                        AssistantTurnState,
                        self._graph.invoke(
                            input_state,
                            config=config,
                            context=context,
                        ),
                    )
            if _is_terminal_state(final_state):
                claim_lease.mark_terminal()
            return final_state

    async def astream(
        self,
        input_state: AssistantTurnState | Command[Any] | None,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> AsyncIterator[GraphStreamPart]:
        """Stream normalized native events from the compiled graph."""

        with self._invocation_claim_scope(
            identity=identity,
            context=context,
        ) as claim_lease:
            terminal_seen = False
            async for part in self._astream_unclaimed(
                input_state,
                identity=identity,
                context=context,
                begin_native=claim_lease.begin_native,
            ):
                if (
                    part.type == "values"
                    and not part.namespace
                    and _is_terminal_state(part.data)
                ):
                    terminal_seen = True
                yield part
            if terminal_seen:
                claim_lease.mark_terminal()

    async def _astream_unclaimed(
        self,
        input_state: AssistantTurnState | Command[Any] | None,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        begin_native: Callable[[], None],
        runnable_config: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[GraphStreamPart]:
        """Stream native events under a claim owned by the public caller."""

        with self._native_tracing(identity):
            with native_graph_trace_scope() as callbacks:
                config = self._runnable_config(
                    identity,
                    callbacks=callbacks,
                    runnable_config=runnable_config,
                )
                begin_native()
                async for raw in self._graph.astream(
                    input_state,
                    config=config,
                    context=context,
                    **ASSISTANT_GRAPH_STREAM_SUBSCRIPTION.native_kwargs(),
                ):
                    yield parse_graph_stream_part(raw)

    async def arun(
        self,
        input_state: AssistantTurnState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Consume one native stream and classify its authoritative snapshot."""

        with self._invocation_claim_scope(
            identity=identity,
            context=context,
        ) as claim_lease:
            result = await self._consume_stream_unclaimed(
                input_state,
                identity=identity,
                context=context,
                begin_native=claim_lease.begin_native,
                part_consumer=part_consumer,
            )
            if _is_terminal_state(result.final_state):
                claim_lease.mark_terminal()
            return result

    async def aresume(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        resume: AssistantResume,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Resume one pending native interrupt on the same thread and a new run."""

        resume_context = replace(context, invocation_kind="resume")
        with self._invocation_claim_scope(
            identity=identity,
            context=resume_context,
        ) as claim_lease:
            result = await self._aresume_claimed(
                identity=identity,
                context=resume_context,
                resume=resume,
                begin_native=claim_lease.begin_native,
                part_consumer=part_consumer,
            )
            if _is_terminal_state(result.final_state):
                claim_lease.mark_terminal()
            return result

    async def areplay(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        request: GraphReplayRequest,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Replay from an owned historical config through the invocation gate."""

        if not isinstance(request, GraphReplayRequest):
            raise TypeError("request must be a GraphReplayRequest")
        replay_context = replace(context, invocation_kind="replay")
        with self._invocation_claim_scope(
            identity=identity,
            context=replay_context,
        ) as claim_lease:
            snapshot = await self._resolve_history_snapshot(identity, request.selector)
            if tuple(getattr(snapshot, "next", ()) or ()) != ("prepare_invocation",):
                raise GraphExecutionError(
                    "graph_checkpoint_not_replayable",
                    "The selected checkpoint has no safe re-entry gate.",
                )
            replay_context = self._replay_context_from_snapshot(
                snapshot,
                identity=identity,
                context=replay_context,
            )
            historical_config = _snapshot_config(snapshot)
            result = await self._consume_stream_unclaimed(
                None,
                identity=identity,
                context=replay_context,
                begin_native=claim_lease.begin_native,
                runnable_config=historical_config,
                part_consumer=part_consumer,
                safe_time_travel_parts=True,
            )
            result = replace(result, checkpoint_config=None)
            if _is_terminal_state(result.final_state):
                claim_lease.mark_terminal()
            return result

    async def afork(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        request: GraphForkRequest,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Create and execute one native branch from an owned checkpoint."""

        if not isinstance(request, GraphForkRequest):
            raise TypeError("request must be a GraphForkRequest")
        fork_context = replace(context, invocation_kind="fork")
        with self._invocation_claim_scope(
            identity=identity,
            context=fork_context,
        ) as claim_lease:
            historical = await self._resolve_history_snapshot(
                identity,
                request.selector,
            )
            try:
                values = fork_patch_for_assistant_state(
                    validate_assistant_turn_state(historical.values),
                    request.patch,
                )
            except Exception as exc:
                raise GraphExecutionError(
                    "graph_checkpoint_incompatible",
                    "Assistant checkpoint cannot be forked by this graph version.",
                ) from exc
            fork_context = self._fork_context_from_snapshot(
                values,
                identity=identity,
                context=fork_context,
            )
            historical_config = _snapshot_config(historical)
            updater = getattr(self._graph, "aupdate_state", None)
            if not callable(updater):
                raise GraphExecutionError(
                    "graph_update_state_api_unavailable",
                    "Compiled graph does not expose native async state updates.",
                )
            claim_lease.begin_native()
            try:
                fork_config = await updater(
                    historical_config,
                    values,
                    as_node="time_travel_anchor",
                )
                getter = getattr(self._graph, "aget_state", None)
                if not callable(getter):
                    raise GraphExecutionError(
                        "graph_state_api_unavailable",
                        "Compiled graph does not expose native async state access.",
                    )
                fork_snapshot = await getter(fork_config)
            except GraphExecutionError:
                raise
            except Exception as exc:
                raise GraphExecutionError(
                    "graph_fork_update_failed",
                    "Native graph branch could not be created safely.",
                ) from exc
            if tuple(getattr(fork_snapshot, "next", ()) or ()) != (
                "prepare_invocation",
            ):
                raise GraphExecutionError(
                    "graph_fork_reentry_missing",
                    "Fork did not enter the invocation gate.",
                )
            result = await self._consume_stream_unclaimed(
                None,
                identity=identity,
                context=fork_context,
                begin_native=lambda: None,
                runnable_config=fork_config,
                part_consumer=part_consumer,
                safe_time_travel_parts=True,
            )
            result = replace(result, checkpoint_config=None)
            if _is_terminal_state(result.final_state):
                claim_lease.mark_terminal()
            return result

    @staticmethod
    def _fork_context_from_snapshot(
        state: AssistantTurnState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> GraphRuntimeContext:
        """Validate fresh invocation identity and frozen refs before branching."""

        persisted_run = state["run"]
        persisted_request = state["request"]
        runtime_state = context.agent_state
        if runtime_state is None:
            raise GraphExecutionError(
                "graph_fork_context_missing",
                "Fork requires invocation-local AgentState.",
            )
        expected_thread = GraphExecutionIdentity.for_assistant_turn(
            agent_id=str(persisted_run["agent_id"]),
            user_id=str(persisted_request["user_id"]),
            session_id=str(persisted_request["session_id"]),
            run_id=identity.run_id,
        ).thread_id
        runtime_request = persisted_request_from_user_request(runtime_state.request)
        runtime_request["capability_refs"] = list(
            assistant_capability_ref_identity(runtime_state)
        )
        if (
            identity.thread_id != expected_thread
            or identity.agent_id != persisted_run["agent_id"]
            or runtime_state.user_id != persisted_request["user_id"]
            or runtime_state.session_id != persisted_request["session_id"]
            or runtime_state.agent_id != persisted_run["agent_id"]
            or runtime_state.run_id != identity.run_id
            or runtime_state.trace_id != persisted_run["trace_id"]
            or runtime_request != persisted_request
        ):
            raise GraphExecutionError(
                "graph_fork_identity_mismatch",
                "Fork identity or runtime context does not own this checkpoint branch.",
            )
        if identity.run_id == persisted_run["run_id"]:
            raise GraphExecutionError(
                "graph_fork_run_id_reused",
                "Fork requires a new invocation run_id.",
            )
        if state["profile"] != context.graph_profile:
            raise GraphExecutionError(
                "graph_profile_mismatch",
                "Fork context profile does not match this checkpoint.",
            )
        try:
            validate_assistant_runtime_refs(state, runtime_state)
        except Exception as exc:
            raise GraphExecutionError(
                "graph_fork_runtime_refs_mismatch",
                "Fork runtime refs do not match this checkpoint.",
            ) from exc
        checkpoint_tools = set(state["catalog"]["available_tool_names"])
        runtime_tools = {
            spec.name for spec in context.tool_executor.registry.list_specs()
        }
        if not checkpoint_tools.issubset(runtime_tools):
            raise GraphExecutionError(
                "graph_fork_catalog_unavailable",
                "Fork checkpoint Tool catalog is unavailable in this runtime.",
            )
        return replace(context, invocation_kind="fork")

    @staticmethod
    def _replay_context_from_snapshot(
        snapshot: Any,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> GraphRuntimeContext:
        """Bind fresh invocation-local identity to owned historical turn facts."""

        values = getattr(snapshot, "values", None)
        try:
            state = validate_assistant_turn_state(values)
        except Exception as exc:
            raise GraphExecutionError(
                "graph_checkpoint_incompatible",
                "Assistant checkpoint cannot be replayed by this graph version.",
            ) from exc
        persisted_run = state["run"]
        persisted_request = state["request"]
        runtime_state = context.agent_state
        if runtime_state is None:
            raise GraphExecutionError(
                "graph_replay_context_missing",
                "Replay requires invocation-local AgentState.",
            )
        expected_thread = GraphExecutionIdentity.for_assistant_turn(
            agent_id=str(persisted_run["agent_id"]),
            user_id=str(persisted_request["user_id"]),
            session_id=str(persisted_request["session_id"]),
            run_id=identity.run_id,
        ).thread_id
        if (
            identity.thread_id != expected_thread
            or identity.agent_id != persisted_run["agent_id"]
            or runtime_state.user_id != persisted_request["user_id"]
            or runtime_state.session_id != persisted_request["session_id"]
            or runtime_state.agent_id != persisted_run["agent_id"]
            or runtime_state.run_id != identity.run_id
            or runtime_state.trace_id != persisted_run["trace_id"]
        ):
            raise GraphExecutionError(
                "graph_replay_identity_mismatch",
                "Replay identity or runtime context does not own this checkpoint.",
            )
        if identity.run_id == persisted_run["run_id"]:
            raise GraphExecutionError(
                "graph_replay_run_id_reused",
                "Replay requires a new invocation run_id.",
            )
        if state["profile"] != context.graph_profile:
            raise GraphExecutionError(
                "graph_profile_mismatch",
                "Replay context profile does not match this checkpoint.",
            )
        return replace(context, invocation_kind="replay")

    async def _aresume_claimed(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        resume: AssistantResume,
        begin_native: Callable[[], None],
        part_consumer: Callable[[GraphStreamPart], object] | None,
    ) -> GraphStreamResult:
        """Validate and resume after the public boundary owns its claim."""

        snapshot = await self.aget_state(identity)
        values = getattr(snapshot, "values", None)
        if not values:
            raise GraphExecutionError(
                "graph_checkpoint_not_found",
                "No assistant checkpoint exists for this thread.",
            )
        try:
            state = validate_assistant_turn_state(values)
        except Exception as exc:
            raise GraphExecutionError(
                "graph_checkpoint_incompatible",
                "Assistant checkpoint cannot be resumed by this graph version.",
            ) from exc
        if state.get("profile") != "standard":
            raise GraphExecutionError(
                "graph_profile_mismatch",
                "Assistant checkpoint profile does not match this graph app.",
            )
        pending_payload = state.get("pending_interrupt")
        if pending_payload is None or not _snapshot_interrupt_objects(snapshot):
            raise GraphExecutionError(
                "graph_interrupt_not_pending",
                "Assistant checkpoint has no pending native interrupt.",
            )
        try:
            request = validate_assistant_interrupt_request(pending_payload)
            validated_resume = validate_assistant_resume(
                resume.model_dump(mode="json")
                if hasattr(resume, "model_dump")
                else resume
            )
            validate_resume_for_interrupt(request, validated_resume)
        except AssistantInterruptContractError as exc:
            raise GraphExecutionError(exc.code, exc.message) from exc

        persisted_run = state["run"]
        persisted_request = state["request"]
        expected_thread = GraphExecutionIdentity.for_assistant_turn(
            agent_id=str(persisted_run["agent_id"]),
            user_id=str(persisted_request["user_id"]),
            session_id=str(persisted_request["session_id"]),
            run_id=identity.run_id,
        ).thread_id
        if (
            identity.thread_id != expected_thread
            or identity.agent_id != persisted_run["agent_id"]
        ):
            raise GraphExecutionError(
                "graph_resume_identity_mismatch",
                "Resume identity does not own the pending assistant thread.",
            )
        if identity.run_id == persisted_run["run_id"]:
            raise GraphExecutionError(
                "graph_resume_run_id_reused",
                "Resume requires a new invocation run_id on the same thread.",
            )
        if context.agent_state is None:
            raise GraphExecutionError(
                "graph_resume_context_missing",
                "Resume requires invocation-local AgentState.",
            )
        if (
            context.agent_state.user_id != persisted_request["user_id"]
            or context.agent_state.session_id != persisted_request["session_id"]
            or context.agent_state.agent_id != persisted_run["agent_id"]
            or context.agent_state.run_id != identity.run_id
        ):
            raise GraphExecutionError(
                "graph_resume_context_mismatch",
                "Resume runtime context does not match the pending assistant thread.",
            )
        # The checkpoint keeps the logical turn trace while each resume has a new
        # graph invocation run.  The native node commits that new run_id before
        # downstream governed work can execute.
        context.agent_state.trace_id = str(persisted_run["trace_id"])

        return await self._consume_stream_unclaimed(
            Command(resume=validated_resume.model_dump(mode="json")),
            identity=identity,
            context=context,
            begin_native=begin_native,
            part_consumer=part_consumer,
        )

    async def aget_state(self, identity: GraphExecutionIdentity) -> Any:
        """Read the native latest StateSnapshot for an assistant thread."""

        getter = getattr(self._graph, "aget_state", None)
        if not callable(getter):
            raise GraphExecutionError(
                "graph_state_api_unavailable",
                "Compiled graph does not expose native async state access.",
            )
        return await getter(identity.runnable_config(), subgraphs=True)

    async def adelete_thread(
        self,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        invocation_claim_store: Any,
    ) -> int:
        """Delete native checkpoints before releasing the retained thread claims."""

        thread_id = stable_assistant_thread_id(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        )
        owner_digest = graph_invocation_owner_digest(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        )
        try:
            invocation_claim_store.begin_thread_delete(
                owner_digest=owner_digest,
                thread_id=thread_id,
            )
        except (
            GraphInvocationClaimConflict,
            GraphInvocationClaimCapacityExceeded,
            GraphInvocationThreadActive,
        ) as exc:
            raise GraphExecutionError(exc.code, str(exc)) from exc
        checkpointer = getattr(self._graph, "checkpointer", None)
        try:
            if checkpointer is not None:
                delete = getattr(checkpointer, "adelete_thread", None)
                if not callable(delete):
                    raise GraphExecutionError(
                        "graph_thread_delete_unavailable",
                        "Configured checkpointer cannot delete an owned thread safely.",
                    )
                await delete(thread_id)
        except BaseException:
            invocation_claim_store.finish_thread_delete(
                owner_digest=owner_digest,
                thread_id=thread_id,
                commit=False,
            )
            raise
        return invocation_claim_store.finish_thread_delete(
            owner_digest=owner_digest,
            thread_id=thread_id,
            commit=True,
        )

    async def aget_state_history(
        self,
        identity: GraphExecutionIdentity,
        limit: int,
    ) -> tuple[Any, ...]:
        """Read bounded newest-first native checkpoint history."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("state history limit must be between 1 and 100")
        history = getattr(self._graph, "aget_state_history", None)
        if not callable(history):
            raise GraphExecutionError(
                "graph_state_history_api_unavailable",
                "Compiled graph does not expose native async state history.",
            )
        return tuple(
            [
                item
                async for item in history(
                    identity.runnable_config(),
                    limit=limit,
                )
            ]
        )

    async def alist_history(
        self,
        identity: GraphExecutionIdentity,
        *,
        limit: int,
        before: GraphCheckpointSelector | None = None,
    ) -> tuple[GraphCheckpointSummary, ...]:
        """Return newest-first, re-entry-safe history without native internals."""

        _validate_history_limit(limit)
        before_config: Mapping[str, Any] | None = None
        if before is not None:
            if not isinstance(before, GraphCheckpointSelector):
                raise TypeError("before must be a GraphCheckpointSelector")
            before_snapshot = await self._resolve_history_snapshot(identity, before)
            before_config = _snapshot_config(before_snapshot)

        summaries: list[GraphCheckpointSummary] = []
        async for snapshot in self._scan_history_snapshots(
            identity,
            before_config=before_config,
        ):
            summary = _history_summary(identity, snapshot)
            if summary is None:
                continue
            summaries.append(summary)
            if len(summaries) == limit:
                break
        return tuple(summaries)

    async def _resolve_history_snapshot(
        self,
        identity: GraphExecutionIdentity,
        selector: GraphCheckpointSelector,
    ) -> Any:
        """Resolve an opaque selector by bounded scanning of its owned thread."""

        if not isinstance(selector, GraphCheckpointSelector):
            raise TypeError("selector must be a GraphCheckpointSelector")
        async for snapshot in self._scan_history_snapshots(identity):
            summary = _history_summary(identity, snapshot)
            if summary is not None and summary.history_ref == selector.history_ref:
                return snapshot
        raise GraphExecutionError(
            "graph_checkpoint_selector_not_found",
            "Graph checkpoint selector is unknown, expired, or not owned by this thread.",
        )

    async def _scan_history_snapshots(
        self,
        identity: GraphExecutionIdentity,
        *,
        before_config: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        """Page native history while enforcing a hard total scan bound."""

        history = getattr(self._graph, "aget_state_history", None)
        if not callable(history):
            raise GraphExecutionError(
                "graph_state_history_api_unavailable",
                "Compiled graph does not expose native async state history.",
            )
        cursor = before_config
        scanned = 0
        while scanned < _HISTORY_SCAN_LIMIT:
            page_limit = min(_HISTORY_PAGE_SIZE, _HISTORY_SCAN_LIMIT - scanned)
            kwargs: dict[str, Any] = {"limit": page_limit}
            if cursor is not None:
                kwargs["before"] = cursor
            try:
                page_items: list[Any] = []
                async for item in history(
                    identity.runnable_config(),
                    **kwargs,
                ):
                    page_items.append(item)
                    if len(page_items) > page_limit:
                        raise GraphExecutionError(
                            "graph_checkpoint_history_invalid",
                            "Native graph history exceeded its requested page limit.",
                        )
                page = tuple(page_items)
            except GraphExecutionError:
                raise
            except Exception as exc:
                raise GraphExecutionError(
                    "graph_checkpoint_history_unavailable",
                    "Native graph history could not be read safely.",
                ) from exc
            if not page:
                break
            for snapshot in page:
                yield snapshot
            scanned += len(page)
            if len(page) < page_limit:
                break
            next_cursor = _snapshot_config(page[-1])
            if cursor is not None and next_cursor == cursor:
                raise GraphExecutionError(
                    "graph_checkpoint_history_invalid",
                    "Native graph history pagination did not advance.",
                )
            cursor = next_cursor

    async def _consume_stream_unclaimed(
        self,
        input_value: AssistantTurnState | Command[Any] | None,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        begin_native: Callable[[], None],
        runnable_config: Mapping[str, Any] | None = None,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
        safe_time_travel_parts: bool = False,
    ) -> GraphStreamResult:
        """Consume the stream, then use native state—not stream shape—as outcome."""

        parts: list[GraphStreamPart] = []
        final_state: AssistantTurnState | object = _MISSING_FINAL_STATE
        final_checkpoint_config: Mapping[str, Any] | None = None
        async for part in self._astream_unclaimed(
            input_value,
            identity=identity,
            context=context,
            begin_native=begin_native,
            runnable_config=runnable_config,
        ):
            if part.type == "values" and not part.namespace:
                final_state = part.data
            if part.type == "checkpoints" and not part.namespace:
                checkpoint_config = (
                    part.data.get("config") if isinstance(part.data, Mapping) else None
                )
                if isinstance(checkpoint_config, Mapping):
                    final_checkpoint_config = checkpoint_config
            public_part = (
                _safe_time_travel_stream_part(part) if safe_time_travel_parts else part
            )
            if public_part is None:
                continue
            parts.append(public_part)
            if part_consumer is not None:
                part_consumer(public_part)
        if getattr(self._graph, "checkpointer", None) is None:
            if final_state is _MISSING_FINAL_STATE:
                raise GraphExecutionError(
                    "graph_final_state_missing",
                    "LangGraph stream ended without root final values.",
                )
            return GraphStreamResult(
                final_state=cast(AssistantTurnState, final_state),
                parts=tuple(parts),
                status="completed",
                interrupts=(),
                checkpoint_config=None,
            )

        if final_checkpoint_config is None:
            raise GraphExecutionError(
                "graph_final_checkpoint_missing",
                "LangGraph stream ended without a root checkpoint config.",
            )
        getter = getattr(self._graph, "aget_state", None)
        if not callable(getter):
            raise GraphExecutionError(
                "graph_state_api_unavailable",
                "Compiled graph does not expose native async state access.",
            )
        snapshot = await getter(
            _checkpoint_lookup_config(final_checkpoint_config),
            subgraphs=True,
        )
        snapshot_values = getattr(snapshot, "values", None)
        if not snapshot_values:
            raise GraphExecutionError(
                "graph_final_state_missing",
                "LangGraph state snapshot contains no root values.",
            )
        try:
            authoritative_state = validate_assistant_turn_state(snapshot_values)
            interrupts = _public_interrupts(snapshot)
        except Exception as exc:
            if isinstance(exc, GraphExecutionError):
                raise
            raise GraphExecutionError(
                "graph_snapshot_invalid",
                "LangGraph state snapshot is incompatible or unsafe.",
            ) from exc
        if authoritative_state["run"]["run_id"] != identity.run_id:
            raise GraphExecutionError(
                "graph_snapshot_identity_mismatch",
                "LangGraph final snapshot does not belong to this invocation.",
            )
        tasks = tuple(getattr(snapshot, "tasks", ()) or ())
        next_nodes = tuple(getattr(snapshot, "next", ()) or ())
        has_pending = bool(tasks or next_nodes)
        if interrupts and has_pending:
            status: Literal["completed", "interrupted"] = "interrupted"
        elif has_pending:
            raise GraphExecutionError(
                "graph_pending_without_interrupt",
                "Graph has pending work without a resumable interrupt.",
            )
        elif interrupts:
            raise GraphExecutionError(
                "graph_interrupt_without_pending_task",
                "Graph exposes an interrupt without pending work.",
            )
        elif authoritative_state["run"]["status"] not in {
            "completed",
            "failed",
            "cancelled",
        }:
            raise GraphExecutionError(
                "graph_terminal_state_invalid",
                "Graph ended without a terminal assistant state.",
            )
        else:
            status = "completed"
        return GraphStreamResult(
            final_state=authoritative_state,
            parts=tuple(parts),
            status=status,
            interrupts=interrupts,
            checkpoint_config=cast(dict[str, Any], snapshot.config),
        )

    @contextmanager
    def _invocation_claim_scope(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> Iterator[_GraphInvocationClaimLease]:
        """Apply one retained claim and exception map to every public API."""

        owner_digest = self._claim_invocation(
            identity=identity,
            context=context,
        )

        def begin_native() -> None:
            context.invocation_claim_store.begin_native(
                owner_digest=owner_digest,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                invocation_token=context.invocation_token,
            )

        def mark_terminal() -> None:
            context.invocation_claim_store.mark_terminal(
                owner_digest=owner_digest,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                invocation_token=context.invocation_token,
            )

        try:
            yield _GraphInvocationClaimLease(
                begin_native=begin_native,
                mark_terminal=mark_terminal,
            )
        except (
            GraphInvocationClaimConflict,
            GraphInvocationClaimCapacityExceeded,
        ) as exc:
            raise GraphExecutionError(exc.code, str(exc)) from exc

    @staticmethod
    def _claim_invocation(
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> str:
        """Claim at the App boundary before tracing or native stream iteration."""

        state = context.agent_state
        if state is None:
            raise GraphExecutionError(
                "graph_invocation_context_missing",
                "Graph invocation requires invocation-local AgentState.",
            )
        expected_thread_id = stable_assistant_thread_id(
            agent_id=state.agent_id,
            user_id=state.user_id,
            session_id=state.session_id,
        )
        if (
            identity.thread_id != expected_thread_id
            or identity.agent_id != state.agent_id
            or identity.run_id != state.run_id
        ):
            raise GraphExecutionError(
                "graph_invocation_identity_mismatch",
                "Graph invocation identity does not match its runtime context.",
            )
        owner_digest = graph_invocation_owner_digest(
            agent_id=state.agent_id,
            user_id=state.user_id,
            session_id=state.session_id,
        )
        try:
            context.invocation_claim_store.claim(
                owner_digest=owner_digest,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                invocation_kind=context.invocation_kind,
                invocation_token=context.invocation_token,
            )
        except (
            GraphInvocationClaimConflict,
            GraphInvocationClaimCapacityExceeded,
        ) as exc:
            raise GraphExecutionError(exc.code, str(exc)) from exc
        return owner_digest

    def _native_tracing(self, identity: GraphExecutionIdentity) -> Any:
        return native_langsmith_tracing(
            self._langsmith_config,
            metadata={
                "run_id": identity.run_id,
                "thread_id": identity.thread_id,
                "agent_id": identity.agent_id,
                "execution_engine": "assistant_turn_graph",
                "graph_profile": "standard",
            },
            tags=["assistant_turn_graph"],
        )

    @staticmethod
    def _runnable_config(
        identity: GraphExecutionIdentity,
        *,
        callbacks: list[Any],
        runnable_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = dict(
            runnable_config
            if runnable_config is not None
            else identity.runnable_config()
        )
        existing_metadata = config.get("metadata")
        metadata = (
            dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
        )
        metadata.update(
            {
                "run_id": identity.run_id,
                "thread_id": identity.thread_id,
                "agent_id": identity.agent_id,
                "execution_engine": "assistant_turn_graph",
                "graph_profile": "standard",
            }
        )
        config["metadata"] = metadata
        existing_tags = list(config.get("tags") or [])
        config["tags"] = list(dict.fromkeys([*existing_tags, "assistant_turn_graph"]))
        existing_callbacks = list(config.get("callbacks") or [])
        config["callbacks"] = [*existing_callbacks, *callbacks]
        return config


def _snapshot_interrupt_objects(snapshot: Any) -> tuple[Any, ...]:
    """Collect native interrupts from root/tasks/subgraphs without namespace keys."""

    collected: list[Any] = list(getattr(snapshot, "interrupts", ()) or ())
    for task in tuple(getattr(snapshot, "tasks", ()) or ()):
        collected.extend(tuple(getattr(task, "interrupts", ()) or ()))
        child = getattr(task, "state", None)
        if child is not None and hasattr(child, "tasks"):
            collected.extend(_snapshot_interrupt_objects(child))
    return tuple(collected)


def _safe_time_travel_stream_part(part: GraphStreamPart) -> GraphStreamPart | None:
    """Project native replay/fork stream data without graph state or native IDs."""

    if part.namespace:
        return None
    if part.type == "updates" and isinstance(part.data, Mapping):
        return GraphUpdatePart(
            type="updates",
            namespace=(),
            data={str(node_name): None for node_name in part.data},
        )
    if part.type == "custom":
        try:
            fact = validate_runtime_product_fact(part.data)
        except Exception:
            return None
        data = fact.model_dump(mode="json")
        if _contains_native_stream_key(data):
            return None
        return GraphCustomPart(type="custom", namespace=(), data=data)
    return None


_NATIVE_STREAM_KEYS = frozenset(
    {
        "checkpoint",
        "checkpoints",
        "checkpoint_id",
        "checkpoint_ns",
        "config",
        "configurable",
        "interrupt_id",
        "ns",
        "state",
        "tasks",
    }
)


def _contains_native_stream_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _NATIVE_STREAM_KEYS
            or _contains_native_stream_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_native_stream_key(item) for item in value)
    return False


def _validate_history_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("state history limit must be between 1 and 100")


def _snapshot_config(snapshot: Any) -> Mapping[str, Any]:
    config = getattr(snapshot, "config", None)
    if not isinstance(config, Mapping):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains an invalid checkpoint config.",
        )
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains an invalid checkpoint config.",
        )
    if (
        not isinstance(configurable.get("thread_id"), str)
        or not isinstance(configurable.get("checkpoint_id"), str)
        or not isinstance(configurable.get("checkpoint_ns"), str)
    ):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains an invalid checkpoint identity.",
        )
    return config


def _checkpoint_lookup_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Keep native checkpoint location while dropping invocation-only run metadata."""

    lookup = dict(config)
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph checkpoint config is invalid.",
        )
    lookup_configurable = dict(configurable)
    lookup_configurable.pop("run_id", None)
    lookup["configurable"] = lookup_configurable
    return lookup


def _history_summary(
    identity: GraphExecutionIdentity,
    snapshot: Any,
) -> GraphCheckpointSummary | None:
    config = _snapshot_config(snapshot)
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains invalid assistant state.",
        )
    next_nodes = tuple(getattr(snapshot, "next", ()) or ())
    if next_nodes == ("__start__",) and not values:
        # LangGraph persists a root pre-input checkpoint whose values are
        # intentionally empty.  It is native history, but never product-
        # selectable and cannot be validated as AssistantTurnState yet.
        return None
    try:
        state = validate_assistant_turn_state(values)
    except Exception as exc:
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains incompatible assistant state.",
        ) from exc
    request = state["request"]
    run = state["run"]
    expected_thread = GraphExecutionIdentity.for_assistant_turn(
        agent_id=str(run["agent_id"]),
        user_id=str(request["user_id"]),
        session_id=str(request["session_id"]),
        run_id=identity.run_id,
    ).thread_id
    configurable = cast(Mapping[str, Any], config["configurable"])
    if (
        identity.thread_id != expected_thread
        or configurable["thread_id"] != identity.thread_id
        or configurable["checkpoint_ns"] != ""
        or identity.agent_id != run["agent_id"]
        or state["profile"] != "standard"
    ):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history does not belong to this assistant graph owner.",
        )

    if next_nodes != ("prepare_invocation",):
        return None
    created_at = _history_created_at(snapshot)
    return GraphCheckpointSummary(
        history_ref=graph_history_ref(
            thread_id=identity.thread_id,
            snapshot_config=config,
        ),
        created_at=created_at,
        status=_history_status(run["status"]),
        next_nodes=next_nodes,
        has_interrupt=bool(_snapshot_interrupt_objects(snapshot)),
        graph_version=str(state["graph_version"]),
        state_schema_version=int(state["state_schema_version"]),
    )


def _history_status(
    value: object,
) -> Literal["running", "waiting_user", "completed", "failed", "cancelled"]:
    if value in {"created", "running"}:
        return "running"
    if value == "waiting_user":
        return "waiting_user"
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    if value == "cancelled":
        return "cancelled"
    raise GraphExecutionError(
        "graph_checkpoint_history_invalid",
        "Native graph history contains an invalid assistant status.",
    )


def _history_created_at(snapshot: Any) -> datetime:
    value = getattr(snapshot, "created_at", None)
    if not isinstance(value, str):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains an invalid creation time.",
        )
    try:
        created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history contains an invalid creation time.",
        ) from exc
    if created_at.tzinfo is None:
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history creation time must include a timezone.",
        )
    return created_at


def _public_interrupts(snapshot: Any) -> tuple[AssistantInterrupt, ...]:
    """Project native Interrupt values and dedupe solely by stable Interrupt.id."""

    by_id: dict[str, AssistantInterrupt] = {}
    for native in _snapshot_interrupt_objects(snapshot):
        interrupt_id = getattr(native, "id", None)
        if not isinstance(interrupt_id, str) or not interrupt_id:
            raise GraphExecutionError(
                "graph_interrupt_id_invalid",
                "Native graph interrupt has no stable id.",
            )
        try:
            request = validate_assistant_interrupt_request(
                getattr(native, "value", None)
            )
            projected = AssistantInterrupt(
                interrupt_id=interrupt_id,
                kind=request.kind,
                prompt=request.prompt,
                action_ref=request.action_ref,
                allowed_resume_kinds=request.allowed_resume_kinds,
            )
        except Exception as exc:
            raise GraphExecutionError(
                "graph_interrupt_payload_invalid",
                "Native graph interrupt payload violates the assistant contract.",
            ) from exc
        existing = by_id.get(interrupt_id)
        if existing is not None and existing != projected:
            raise GraphExecutionError(
                "graph_interrupt_id_conflict",
                "One native interrupt id resolved to conflicting payloads.",
            )
        by_id[interrupt_id] = projected
    return tuple(by_id.values())


def _is_terminal_state(value: object) -> bool:
    try:
        state = validate_assistant_turn_state(value)
    except Exception:
        return False
    return state["run"]["status"] in {"completed", "failed", "cancelled"}


def _default_langsmith_config() -> LangSmithConfig:
    try:
        return LangSmithConfig.from_env()
    except Exception:
        return LangSmithConfig(enabled=False)
