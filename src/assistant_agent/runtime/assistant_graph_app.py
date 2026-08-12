"""Stable compiled application for the assistant turn graph."""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterator, Literal, cast

from langgraph.types import Command

from assistant_agent.runtime.assistant_loop_graph import (
    build_assistant_loop_graph,
    build_namespaced_assistant_loop_graph,
)
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    validate_assistant_turn_state,
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
    GraphInvocationClaimResult,
    graph_invocation_owner_digest,
)
from assistant_agent.runtime.graph_time_travel import (
    GraphCheckpointSelector,
    GraphCheckpointSummary,
    graph_history_ref,
)
from assistant_agent.runtime.tool_operation_barrier import stable_assistant_thread_id
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


@dataclass(frozen=True)
class GraphStreamPart:
    """One normalized item from a LangGraph v2 execution stream."""

    type: str
    namespace: tuple[str, ...]
    data: Any


@dataclass(frozen=True)
class GraphStreamResult:
    """Authoritative graph outcome plus the native stream that produced it."""

    final_state: AssistantTurnState
    parts: tuple[GraphStreamPart, ...]
    status: Literal["completed", "interrupted"] = "completed"
    interrupts: tuple[AssistantInterrupt, ...] = ()
    checkpoint_config: dict[str, Any] | None = None


@dataclass
class _GraphInvocationClaimScope:
    """One App-boundary claim shared by sync and async native execution."""

    identity: GraphExecutionIdentity
    context: GraphRuntimeContext
    owner_digest: str
    claim_result: GraphInvocationClaimResult
    checkpointed: bool
    native_started: bool = False

    def mark_native_started(self) -> None:
        self.native_started = True

    def release_before_native_start(self) -> None:
        if self.claim_result != "claimed" or self.native_started or self.checkpointed:
            return
        self._release()

    def release_terminal(self, final_state: object) -> None:
        if self.checkpointed:
            return
        try:
            state = validate_assistant_turn_state(final_state)
        except Exception:
            return
        if state["run"]["status"] in {"completed", "failed", "cancelled"}:
            self._release()

    def _release(self) -> None:
        self.context.invocation_claim_store.release(
            owner_digest=self.owner_digest,
            thread_id=self.identity.thread_id,
            run_id=self.identity.run_id,
            invocation_token=self.context.invocation_token,
        )


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
        ) as claim_scope:
            with self._native_tracing(identity):
                with native_graph_trace_scope() as callbacks:
                    config = self._runnable_config(identity, callbacks=callbacks)
                    claim_scope.mark_native_started()
                    final_state = cast(
                        AssistantTurnState,
                        self._graph.invoke(
                            input_state,
                            config=config,
                            context=context,
                        ),
                    )
            claim_scope.release_terminal(final_state)
            return final_state

    async def astream(
        self,
        input_state: AssistantTurnState | Command[Any],
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        _stream_started: Callable[[], None] | None = None,
    ) -> AsyncIterator[GraphStreamPart]:
        """Stream normalized native events from the compiled graph."""

        with self._native_tracing(identity):
            with native_graph_trace_scope() as callbacks:
                config = self._runnable_config(identity, callbacks=callbacks)
                if _stream_started is not None:
                    _stream_started()
                async for raw in self._graph.astream(
                    input_state,
                    config=config,
                    context=context,
                    stream_mode=[
                        "values",
                        "updates",
                        "messages",
                        "custom",
                        "tasks",
                        "checkpoints",
                    ],
                    subgraphs=True,
                    version="v2",
                ):
                    yield GraphStreamPart(
                        type=str(raw["type"]),
                        namespace=tuple(raw.get("ns") or ()),
                        data=raw.get("data"),
                    )

    async def arun(
        self,
        input_state: AssistantTurnState,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Consume one native stream and classify its authoritative snapshot."""

        return await self._consume_stream(
            input_state,
            identity=identity,
            context=context,
            part_consumer=part_consumer,
        )

    async def aresume(
        self,
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        resume: AssistantResume,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Resume one pending native interrupt on the same thread and a new run."""

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
        if identity.thread_id != expected_thread or identity.agent_id != persisted_run[
            "agent_id"
        ]:
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

        return await self._consume_stream(
            Command(resume=validated_resume.model_dump(mode="json")),
            identity=identity,
            context=replace(context, invocation_kind="resume"),
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

    async def aget_state_history(
        self,
        identity: GraphExecutionIdentity,
        limit: int,
    ) -> tuple[Any, ...]:
        """Read bounded newest-first native checkpoint history."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("state history limit must be between 1 and 100")
        history = getattr(self._graph, "aget_state_history", None)
        if not callable(history):
            raise GraphExecutionError(
                "graph_state_history_api_unavailable",
                "Compiled graph does not expose native async state history.",
            )
        return tuple([
            item
            async for item in history(
                identity.runnable_config(),
                limit=limit,
            )
        ])

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

    async def _consume_stream(
        self,
        input_value: AssistantTurnState | Command[Any],
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
        part_consumer: Callable[[GraphStreamPart], object] | None = None,
    ) -> GraphStreamResult:
        """Consume the stream, then use native state—not stream shape—as outcome."""

        with self._invocation_claim_scope(
            identity=identity,
            context=context,
        ) as claim_scope:
            parts: list[GraphStreamPart] = []
            final_state: AssistantTurnState | object = _MISSING_FINAL_STATE
            async for part in self.astream(
                input_value,
                identity=identity,
                context=context,
                _stream_started=claim_scope.mark_native_started,
            ):
                parts.append(part)
                if part_consumer is not None:
                    part_consumer(part)
                if part.type == "values" and not part.namespace:
                    final_state = part.data
            if getattr(self._graph, "checkpointer", None) is None:
                if final_state is _MISSING_FINAL_STATE:
                    raise GraphExecutionError(
                        "graph_final_state_missing",
                        "LangGraph stream ended without root final values.",
                    )
                result = GraphStreamResult(
                    final_state=cast(AssistantTurnState, final_state),
                    parts=tuple(parts),
                    status="completed",
                    interrupts=(),
                    checkpoint_config=None,
                )
                claim_scope.release_terminal(result.final_state)
                return result

            snapshot = await self.aget_state(identity)
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
    ) -> Iterator[_GraphInvocationClaimScope]:
        """Apply one preflight, exception map, and release policy to every API."""

        owner_digest, claim_result = self._claim_invocation(
            identity=identity,
            context=context,
        )
        scope = _GraphInvocationClaimScope(
            identity=identity,
            context=context,
            owner_digest=owner_digest,
            claim_result=claim_result,
            checkpointed=getattr(self._graph, "checkpointer", None) is not None,
        )
        try:
            yield scope
        except (GraphInvocationClaimConflict, GraphInvocationClaimCapacityExceeded) as exc:
            scope.release_before_native_start()
            raise GraphExecutionError(exc.code, str(exc)) from exc
        except BaseException:
            scope.release_before_native_start()
            raise

    @staticmethod
    def _claim_invocation(
        *,
        identity: GraphExecutionIdentity,
        context: GraphRuntimeContext,
    ) -> tuple[str, GraphInvocationClaimResult]:
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
            result = context.invocation_claim_store.claim(
                owner_digest=owner_digest,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
                invocation_kind=context.invocation_kind,
                invocation_token=context.invocation_token,
            )
        except (GraphInvocationClaimConflict, GraphInvocationClaimCapacityExceeded) as exc:
            raise GraphExecutionError(exc.code, str(exc)) from exc
        return owner_digest, result

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
    ) -> dict[str, Any]:
        config: dict[str, Any] = dict(identity.runnable_config())
        config["metadata"] = {
            "run_id": identity.run_id,
            "thread_id": identity.thread_id,
            "agent_id": identity.agent_id,
            "execution_engine": "assistant_turn_graph",
            "graph_profile": "standard",
        }
        config["tags"] = ["assistant_turn_graph"]
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
        or identity.agent_id != run["agent_id"]
        or state["profile"] != "standard"
    ):
        raise GraphExecutionError(
            "graph_checkpoint_history_invalid",
            "Native graph history does not belong to this assistant graph owner.",
        )

    next_nodes = tuple(getattr(snapshot, "next", ()) or ())
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


def _history_status(value: object) -> Literal[
    "running", "waiting_user", "completed", "failed", "cancelled"
]:
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
            request = validate_assistant_interrupt_request(getattr(native, "value", None))
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


def _default_langsmith_config() -> LangSmithConfig:
    try:
        return LangSmithConfig.from_env()
    except Exception:
        return LangSmithConfig(enabled=False)
