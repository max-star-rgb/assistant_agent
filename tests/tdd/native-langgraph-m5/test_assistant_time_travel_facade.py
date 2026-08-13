from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import wraps

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.identity import RequestIdentity
from assistant_agent.context.models import ContextSourceIssue, ContextSourceResult
from assistant_agent.memory.plugins.contracts import (
    MemoryContextContribution,
    MemoryContextItem,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    MemorySessionCloseResult,
    MemorySessionOpenResult,
    MemoryTurnIngestionResult,
)
from assistant_agent.memory.plugins.host import MemoryPluginHost
from assistant_agent.memory.models import SessionMemorySnapshot
from assistant_agent.memory.plugins.registry import (
    MemoryPluginRegistrationRecord,
    MemoryPluginRegistry,
)
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.runtime.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.runtime.cancellation import AgentRunCancelled
from assistant_agent.runtime.assistant_graph_app import GraphExecutionError
from assistant_agent.runtime.assistant_graph_state import (
    AssistantStateCompatibilityError,
    assistant_turn_state_from_request,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.assistant_interrupts import (
    AssistantApproveResume,
    AssistantInterruptRequest,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_time_travel import (
    GraphForkRequest,
    GraphReplayRequest,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.run_history import RunHistoryStore
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.output_models import NativeToolCall
from tests.core.support import (
    CancelledToken,
    ProbeTool,
    ScriptedChatAdapter,
    offline_config,
    sealed_registry,
)


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class _RecordingMemoryPlugin:
    descriptor = MemoryPluginDescriptor(
        plugin_id="facade-memory",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities={"text"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=False,
            supports_idempotent_ingestion=True,
        ),
    )

    def __init__(self) -> None:
        self.calls = {"open": 0, "prepare": 0, "ingest": 0}

    def open_session(self, request) -> MemorySessionOpenResult:
        self.calls["open"] += 1
        return MemorySessionOpenResult(
            status="ready",
            initial_contribution=MemoryContextContribution(status="succeeded"),
        )

    def prepare_context(self, request) -> MemoryContextContribution:
        self.calls["prepare"] += 1
        return MemoryContextContribution(
            status="succeeded",
            items=[
                MemoryContextItem(
                    memory_id="facade-memory-item",
                    text="frozen facade memory",
                    source="semantic",
                )
            ],
        )

    def ingest_turn(self, request) -> MemoryTurnIngestionResult:
        self.calls["ingest"] += 1
        return MemoryTurnIngestionResult(status="accepted")

    def close_session(self, request) -> MemorySessionCloseResult:
        return MemorySessionCloseResult(status="closed")


def _recording_memory_service(plugin: _RecordingMemoryPlugin) -> LongTermMemoryService:
    registry = MemoryPluginRegistry(
        records=[
            MemoryPluginRegistrationRecord(
                descriptor=plugin.descriptor,
                source="task7-tdd",
                enabled=True,
                active=True,
            )
        ],
        active_plugin=plugin,
    )
    return LongTermMemoryService(host=MemoryPluginHost(registry=registry))


def test_runtime_app_resolves_process_runtime_once() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
    )
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return runtime

    try:
        app = AssistantRuntimeApp(runtime_factory=factory)
        assert app.runtime is runtime
        assert app.runtime is runtime
    finally:
        runtime.close()

    assert calls == 1


def test_legacy_v2_checkpoint_without_provenance_fails_closed() -> None:
    legacy = assistant_turn_state_from_request(
        UserRequest(user_id="legacy-user", session_id="legacy-session", text="legacy"),
        run_id="legacy-run",
        trace_id="legacy-trace",
    )
    legacy["state_schema_version"] = 2
    legacy.pop("memory_origin_run_id")
    legacy.pop("turn_provenance")

    with pytest.raises(AssistantStateCompatibilityError):
        validate_assistant_turn_state(legacy)


def test_host_continuation_binding_is_owner_origin_and_ref_bound() -> None:
    plugin = _RecordingMemoryPlugin()
    service = _recording_memory_service(plugin)
    owner = RequestIdentity.for_user(
        user_id="host-owner", agent_id="agent.default", session_id="host-session"
    )
    origin = AgentState.from_request(
        UserRequest(user_id=owner.user_id, session_id=owner.session_id, text="origin"),
        run_id="host-origin",
    )
    service.host._prepare_context_once = lambda **kwargs: SessionMemorySnapshot(  # noqa: SLF001
        plugin_id=plugin.descriptor.plugin_id,
        memories=[
            MemoryContextItem(
                memory_id="owner-memory", text="owner memory", source="semantic"
            )
        ],
    )
    service.prepare_context(state=origin, trace_store=None, cancel_token=None)
    provider_calls = dict(plugin.calls)

    def target(user_id: str = owner.user_id) -> AgentState:
        return AgentState.from_request(
            UserRequest(user_id=user_id, session_id=owner.session_id, text="target"),
            run_id="host-target",
        )

    wrong_owner = target("other-owner")
    with pytest.raises(ValueError):
        service.attach_continuation_snapshot(
            wrong_owner,
            origin_identity=owner,
            origin_run_id="host-origin",
            expected_memory_refs=(("owner-memory", "semantic"),),
        )
    assert wrong_owner.session_memory_snapshot is None

    for origin_run_id, refs in (
        ("wrong-origin", (("owner-memory", "semantic"),)),
        ("host-origin", (("tampered-memory", "semantic"),)),
    ):
        candidate = target()
        assert (
            service.attach_continuation_snapshot(
                candidate,
                origin_identity=owner,
                origin_run_id=origin_run_id,
                expected_memory_refs=refs,
            )
            is None
        )
        assert candidate.session_memory_snapshot is None

    fresh = _recording_memory_service(_RecordingMemoryPlugin())
    fresh_target = target()
    assert (
        fresh.attach_continuation_snapshot(
            fresh_target,
            origin_identity=owner,
            origin_run_id="host-origin",
            expected_memory_refs=(("owner-memory", "semantic"),),
        )
        is None
    )
    assert fresh_target.session_memory_snapshot is None

    attached = target()
    assert (
        service.attach_continuation_snapshot(
            attached,
            origin_identity=owner,
            origin_run_id="host-origin",
            expected_memory_refs=(("owner-memory", "semantic"),),
        )
        is not None
    )
    service.release_run_context(identity=owner, run_id="host-origin")
    after_terminal = target()
    assert (
        service.attach_continuation_snapshot(
            after_terminal,
            origin_identity=owner,
            origin_run_id="host-origin",
            expected_memory_refs=(("owner-memory", "semantic"),),
        )
        is None
    )
    assert plugin.calls == provider_calls


class _StableContextCoordinator:
    def load_once(self, request) -> ContextSourceResult:
        return ContextSourceResult(
            issues=[
                ContextSourceIssue(
                    code="stable-context-issue",
                    source_ref="stable-context-ref",
                    public_message="stable context unavailable",
                )
            ]
        )


@_async_test
async def test_runtime_app_replay_uses_shared_graph_without_memory_lifecycle() -> None:
    """A product facade that rebuilds Runtime would lose the selected checkpoint."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="original answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    request = UserRequest(
        user_id="facade-user",
        session_id="facade-session",
        text="facade request",
    )
    owner = RequestIdentity.for_user(
        user_id=request.user_id,
        agent_id=runtime.agent_id,
        session_id=request.session_id,
    )
    memory_calls = {"initialize_session": 0, "prepare_context": 0, "ingest_turn": 0}
    originals = {
        "initialize_session": runtime.long_term_memory_service.initialize_session,
        "prepare_context": runtime.long_term_memory_service.prepare_context,
        "ingest_turn": runtime.long_term_memory_service.enqueue_completed_turn,
    }

    def track(name):
        def tracked(*args, **kwargs):
            memory_calls[name] += 1
            return originals[name](*args, **kwargs)

        return tracked

    runtime.long_term_memory_service.initialize_session = track("initialize_session")
    runtime.long_term_memory_service.prepare_context = track("prepare_context")
    runtime.long_term_memory_service.enqueue_completed_turn = track("ingest_turn")
    compiled_graph = runtime.assistant_graph_app.graph
    try:
        original = await runtime.arun_state(request, run_id="run-facade-origin")
        history = await app.list_turn_history(owner, limit=10)
        before_replay = dict(memory_calls)
        replayed = await app.replay_turn(
            owner,
            GraphReplayRequest(
                selector={"history_ref": history[1].history_ref},
            ),
            run_id="run-facade-replay",
        )
    finally:
        runtime.close()

    assert replayed.run_id == "run-facade-replay"
    assert replayed.trace_id == original.trace_id
    assert runtime.assistant_graph_app.graph is compiled_graph
    assert {key: memory_calls[key] - before_replay[key] for key in memory_calls} == {
        "initialize_session": 0,
        "prepare_context": 0,
        "ingest_turn": 0,
    }


@_async_test
async def test_replay_reconstructs_stable_non_memory_context_refs() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        context_source_coordinator=_StableContextCoordinator(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    owner = RequestIdentity.for_user(
        user_id="context-ref-user",
        agent_id=runtime.agent_id,
        session_id="context-ref-session",
    )
    try:
        await runtime.arun_state(
            UserRequest(
                user_id=owner.user_id, session_id=owner.session_id, text="context ref"
            ),
            run_id="context-ref-origin",
        )
        history = await app.list_turn_history(owner, limit=10)
        replayed = await app.replay_turn(
            owner,
            GraphReplayRequest(selector={"history_ref": history[1].history_ref}),
            run_id="context-ref-replay",
        )
    finally:
        runtime.close()

    assert replayed.run_id == "context-ref-replay"
    assert replayed.context_source_result.issues[0].source_ref == "stable-context-ref"


@_async_test
async def test_runtime_app_fork_uses_shared_graph_without_memory_lifecycle() -> None:
    """Forking through a new composition root would not preserve graph ownership."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="original answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    request = UserRequest(
        user_id="facade-fork-user",
        session_id="facade-fork-session",
        text="original request",
    )
    owner = RequestIdentity.for_user(
        user_id=request.user_id,
        agent_id=runtime.agent_id,
        session_id=request.session_id,
    )
    calls = {"initialize_session": 0, "prepare_context": 0, "ingest_turn": 0}
    names = {
        "initialize_session": "initialize_session",
        "prepare_context": "prepare_context",
        "ingest_turn": "enqueue_completed_turn",
    }
    for key, attribute in names.items():
        original = getattr(runtime.long_term_memory_service, attribute)

        def tracked(*args, _key=key, _original=original, **kwargs):
            calls[_key] += 1
            return _original(*args, **kwargs)

        setattr(runtime.long_term_memory_service, attribute, tracked)
    compiled_graph = runtime.assistant_graph_app.graph
    try:
        original_state = await runtime.arun_state(request, run_id="run-fork-origin")
        history = await app.list_turn_history(owner, limit=10)
        before_fork = dict(calls)
        forked = await app.fork_turn(
            owner,
            GraphForkRequest(
                selector={"history_ref": history[1].history_ref},
                patch={"response_style": "concise"},
            ),
            run_id="run-facade-fork",
        )
    finally:
        runtime.close()

    assert forked.run_id == "run-facade-fork"
    assert forked.trace_id == original_state.trace_id
    assert forked.request.response_style == "concise"
    assert runtime.assistant_graph_app.graph is compiled_graph
    assert {key: calls[key] - before_fork[key] for key in calls} == {
        "initialize_session": 0,
        "prepare_context": 0,
        "ingest_turn": 0,
    }


@_async_test
async def test_resume_skips_memory_preparation_and_ingests_terminal_once() -> None:
    """Re-preparing Memory on resume would duplicate lifecycle work for one turn."""

    tool = ProbeTool()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="facade-resume-action",
                            name=tool.name,
                            arguments={"value": "resume sentinel"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="resumed answer",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        allow_interrupt=True,
    )
    request = UserRequest(
        user_id="facade-resume-user",
        session_id="facade-resume-session",
        text="resume request",
    )
    calls = {"initialize_session": 0, "prepare_context": 0, "ingest_turn": 0}
    names = {
        "initialize_session": "initialize_session",
        "prepare_context": "prepare_context",
        "ingest_turn": "enqueue_completed_turn",
    }
    for key, attribute in names.items():
        original = getattr(runtime.long_term_memory_service, attribute)

        def tracked(*args, _key=key, _original=original, **kwargs):
            calls[_key] += 1
            return _original(*args, **kwargs)

        setattr(runtime.long_term_memory_service, attribute, tracked)
    try:
        waiting = await runtime.arun_state(
            request,
            run_id="run-before-resume",
            interrupt_request=AssistantInterruptRequest(
                kind="approval",
                prompt="approve sentinel",
                action_ref="facade-resume-action",
                allowed_resume_kinds=("approve", "reject"),
            ),
        )
        before_resume = dict(calls)
        resumed = await runtime.aresume_state(
            request,
            resume=AssistantApproveResume(action_ref="facade-resume-action"),
            run_id="run-after-resume",
        )
    finally:
        runtime.close()

    assert waiting.status == "waiting_user"
    assert resumed.status == "completed"
    assert resumed.run_id == "run-after-resume"
    assert {key: calls[key] - before_resume[key] for key in calls} == {
        "initialize_session": 0,
        "prepare_context": 0,
        "ingest_turn": 1,
    }


@_async_test
async def test_history_rejects_runtime_without_saver() -> None:
    """An empty history must not masquerade as enabled time travel."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    try:
        with pytest.raises(GraphExecutionError) as exc:
            await app.list_turn_history(
                RequestIdentity.for_user(
                    user_id="no-saver-user",
                    agent_id=runtime.agent_id,
                    session_id="no-saver-session",
                ),
                limit=10,
            )
    finally:
        runtime.close()

    assert exc.value.code == "graph_checkpointer_required"


@_async_test
async def test_history_selector_is_bound_to_owner_session() -> None:
    """A selector discovered in one session must not select another thread."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="owner answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    owner = RequestIdentity.for_user(
        user_id="selector-user",
        agent_id=runtime.agent_id,
        session_id="selector-owner-session",
    )
    try:
        await runtime.arun_state(
            UserRequest(
                user_id=owner.user_id,
                session_id=owner.session_id,
                text="owner request",
            ),
            run_id="selector-origin",
        )
        history = await app.list_turn_history(owner, limit=10)
        other_session = owner.model_copy(update={"session_id": "other-session"})
        with pytest.raises(GraphExecutionError) as exc:
            await app.replay_turn(
                other_session,
                GraphReplayRequest(
                    selector={"history_ref": history[1].history_ref},
                ),
                run_id="selector-cross-session",
            )
    finally:
        runtime.close()

    assert exc.value.code == "graph_checkpoint_selector_not_found"


@_async_test
async def test_invalid_selector_retains_run_claim_before_preflight() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    owner = RequestIdentity.for_user(
        user_id="claim-user",
        agent_id=runtime.agent_id,
        session_id="claim-session",
    )
    request = GraphReplayRequest(selector={"history_ref": "ghr_" + "a" * 32})
    try:
        with pytest.raises(GraphExecutionError) as first:
            await app.replay_turn(owner, request, run_id="claimed-invalid-selector")
        with pytest.raises(GraphExecutionError) as reused:
            await app.replay_turn(owner, request, run_id="claimed-invalid-selector")
    finally:
        runtime.close()

    assert first.value.code == "graph_checkpoint_selector_not_found"
    assert reused.value.code == "graph_invocation_run_id_reused"


@_async_test
async def test_continuation_rejects_unresolvable_checkpoint_context_refs() -> None:
    """A continuation cannot silently replace frozen refs with current Memory/context."""

    saver = InMemorySaver()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="ref answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=saver,
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    owner = RequestIdentity.for_user(
        user_id="ref-user",
        agent_id=runtime.agent_id,
        session_id="ref-session",
    )
    try:
        await runtime.arun_state(
            UserRequest(user_id=owner.user_id, session_id=owner.session_id, text="ref"),
            run_id="ref-origin",
        )
        identity = runtime._graph_identity_for_owner(owner, run_id="ref-inspect")
        snapshot = await runtime.assistant_graph_app.aget_state(identity)
        tampered = deepcopy(snapshot.values)
        tampered["context_refs"] = [
            {
                "kind": "memory",
                "ref": "missing-frozen-memory-ref",
                "source": "long_term",
                "version": None,
                "status_code": None,
            }
        ]
        await runtime.assistant_graph_app.graph.aupdate_state(
            snapshot.config,
            tampered,
            as_node="time_travel_anchor",
        )
        history = await app.list_turn_history(owner, limit=10)
        with pytest.raises(GraphExecutionError) as exc:
            await app.replay_turn(
                owner,
                GraphReplayRequest(
                    selector={"history_ref": history[0].history_ref},
                ),
                run_id="ref-replay",
            )
    finally:
        runtime.close()

    assert exc.value.code == "graph_continuation_context_refs_unavailable"


@_async_test
async def test_replay_interrupt_waits_and_later_resume_keeps_zero_ingestion() -> None:
    tool = ProbeTool()
    plugin = _RecordingMemoryPlugin()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="origin-action",
                            name=tool.name,
                            arguments={"value": "origin"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="origin-action",
                            name=tool.name,
                            arguments={"value": "derived"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="derived resumed",
                ),
            ]
        ),
        long_term_memory_service=_recording_memory_service(plugin),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        allow_interrupt=True,
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    enqueue_calls = 0
    original_enqueue = runtime.long_term_memory_service.enqueue_completed_turn

    def track_enqueue(*args, **kwargs):
        nonlocal enqueue_calls
        enqueue_calls += 1
        return original_enqueue(*args, **kwargs)

    runtime.long_term_memory_service.enqueue_completed_turn = track_enqueue
    request = UserRequest(
        user_id="derived-memory-user",
        session_id="derived-memory-session",
        text="derive and pause",
    )
    owner = RequestIdentity.for_user(
        user_id=request.user_id,
        agent_id=runtime.agent_id,
        session_id=request.session_id,
    )
    try:
        origin = await runtime.arun_state(
            request,
            run_id="derived-origin",
            interrupt_request=AssistantInterruptRequest(
                kind="approval",
                prompt="approve",
                action_ref="origin-action",
                allowed_resume_kinds=("approve", "reject"),
            ),
        )
        history = await app.list_turn_history(owner, limit=20)
        before = dict(plugin.calls)
        replayed = await app.replay_turn(
            owner,
            GraphReplayRequest(selector={"history_ref": history[-1].history_ref}),
            run_id="derived-replay",
        )
        resumed = await runtime.aresume_state(
            request,
            resume=AssistantApproveResume(action_ref="origin-action"),
            run_id="derived-resume",
        )
        probe = AgentState.from_request(
            request, run_id="derived-origin-probe", agent_id=runtime.agent_id
        )
        expected_refs = tuple(
            (item.memory_id, item.source)
            for item in origin.session_memory_snapshot.memories
        )
        origin_still_bound = (
            runtime.long_term_memory_service.attach_continuation_snapshot(
                probe,
                origin_identity=owner,
                origin_run_id="derived-origin",
                expected_memory_refs=expected_refs,
            )
        )
    finally:
        runtime.close()

    assert origin.status == "waiting_user"
    assert replayed.status == "waiting_user"
    assert resumed.status == "completed"
    assert enqueue_calls == 0
    assert origin_still_bound == origin.session_memory_snapshot
    assert {key: plugin.calls[key] - before[key] for key in plugin.calls} == {
        "open": 0,
        "prepare": 0,
        "ingest": 0,
    }


@_async_test
async def test_memory_enabled_resume_attaches_exact_host_context_without_recall() -> (
    None
):
    tool = ProbeTool()
    plugin = _RecordingMemoryPlugin()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="memory-action",
                            name=tool.name,
                            arguments={"value": "memory"},
                        )
                    ],
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="memory resumed",
                ),
            ]
        ),
        long_term_memory_service=_recording_memory_service(plugin),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        allow_interrupt=True,
    )
    request = UserRequest(
        user_id="frozen-memory-user",
        session_id="frozen-memory-session",
        text="freeze memory",
    )
    enqueues = 0
    original_enqueue = runtime.long_term_memory_service.enqueue_completed_turn

    def track_enqueue(*args, **kwargs):
        nonlocal enqueues
        enqueues += 1
        return original_enqueue(*args, **kwargs)

    runtime.long_term_memory_service.enqueue_completed_turn = track_enqueue
    runtime.long_term_memory_service.host._prepare_context_once = lambda **kwargs: (  # noqa: SLF001
        SessionMemorySnapshot(
            plugin_id=plugin.descriptor.plugin_id,
            memories=[
                MemoryContextItem(
                    memory_id="facade-memory-item",
                    text="frozen facade memory",
                    source="semantic",
                )
            ],
        )
    )
    try:
        waiting = await runtime.arun_state(
            request,
            run_id="frozen-memory-origin",
            interrupt_request=AssistantInterruptRequest(
                kind="approval",
                prompt="approve memory",
                action_ref="memory-action",
                allowed_resume_kinds=("approve", "reject"),
            ),
        )
        expected_snapshot = waiting.session_memory_snapshot.model_copy(deep=True)
        before = dict(plugin.calls)
        resumed = await runtime.aresume_state(
            request,
            resume=AssistantApproveResume(action_ref="memory-action"),
            run_id="frozen-memory-resume",
        )
    finally:
        runtime.close()

    assert resumed.status == "completed"
    assert [item.memory_id for item in expected_snapshot.memories] == [
        "facade-memory-item"
    ]
    assert resumed.session_memory_snapshot == expected_snapshot
    assert {key: plugin.calls[key] - before[key] for key in ("open", "prepare")} == {
        "open": 0,
        "prepare": 0,
    }
    assert enqueues == 1


@_async_test
async def test_time_travel_effect_preflight_precedes_product_lifecycle(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_history = RunHistoryStore(tmp_path / "runs.jsonl")
    sessions = InMemorySessionStore()
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="origin",
                )
            ]
        ),
        session_store=sessions,
        run_history=run_history,
        checkpointer=InMemorySaver(),
    )
    app = AssistantRuntimeApp(runtime_factory=lambda: runtime)
    owner = RequestIdentity.for_user(
        user_id="effect-user", agent_id=runtime.agent_id, session_id="effect-session"
    )
    try:
        await runtime.arun_state(
            UserRequest(
                user_id=owner.user_id, session_id=owner.session_id, text="origin"
            ),
            run_id="effect-origin",
        )
        history = await app.list_turn_history(owner, limit=10)
        baseline_history = tuple(run_history.read_all())
        baseline_session = sessions.get(owner.user_id, owner.session_id)
        resolve_calls = 0
        original_resolve = runtime.assistant_graph_app._resolve_history_snapshot

        async def track_resolve(*args, **kwargs):
            nonlocal resolve_calls
            resolve_calls += 1
            return await original_resolve(*args, **kwargs)

        def reject_effect(*args, **kwargs):
            raise GraphExecutionError(
                "graph_time_travel_effect_outcome_unknown",
                "unknown effect",
            )

        monkeypatch.setattr(
            runtime.assistant_graph_app,
            "_guard_time_travel_effects",
            reject_effect,
        )
        monkeypatch.setattr(
            runtime.assistant_graph_app,
            "_resolve_history_snapshot",
            track_resolve,
        )
        with pytest.raises(GraphExecutionError) as captured:
            await app.replay_turn(
                owner,
                GraphReplayRequest(selector={"history_ref": history[1].history_ref}),
                run_id="effect-rejected",
            )
        with pytest.raises(GraphExecutionError) as reused:
            await app.replay_turn(
                owner,
                GraphReplayRequest(selector={"history_ref": history[1].history_ref}),
                run_id="effect-rejected",
            )
    finally:
        runtime.close()

    assert captured.value.code == "graph_time_travel_effect_outcome_unknown"
    assert reused.value.code == "graph_invocation_run_id_reused"
    assert resolve_calls == 1
    assert tuple(run_history.read_all()) == baseline_history
    assert sessions.get(owner.user_id, owner.session_id) == baseline_session


@_async_test
async def test_native_started_failure_does_not_publish_terminal_lifecycle(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_history = RunHistoryStore(tmp_path / "runs.jsonl")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter([]),
        session_store=InMemorySessionStore(),
        run_history=run_history,
        checkpointer=InMemorySaver(),
    )
    graph_type = type(runtime.assistant_graph_app.graph)

    async def fail_after_native_start(*args, **kwargs):
        if False:
            yield None
        raise GraphExecutionError("native-stream-failed", "native failed")

    monkeypatch.setattr(graph_type, "astream", fail_after_native_start)
    try:
        with pytest.raises(GraphExecutionError):
            await runtime.arun_state(
                UserRequest(
                    user_id="native-failure-user",
                    session_id="native-failure-session",
                    text="fail natively",
                ),
                run_id="native-failure-run",
            )
    finally:
        runtime.close()

    records = run_history.read_all()
    assert [record.status for record in records] == ["started"]


@_async_test
async def test_bound_continuation_handle_is_single_consumer() -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="origin",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="replay",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    owner = RequestIdentity.for_user(
        user_id="single-handle-user",
        agent_id=runtime.agent_id,
        session_id="single-handle-session",
    )
    try:
        await runtime.arun_state(
            UserRequest(
                user_id=owner.user_id, session_id=owner.session_id, text="origin"
            ),
            run_id="single-handle-origin",
        )
        history = await runtime.alist_history(owner, limit=10)
        prepared = await runtime._prepare_graph_continuation(
            owner,
            run_id="single-handle-replay",
            invocation_kind="replay",
            request=GraphReplayRequest(
                selector={"history_ref": history[1].history_ref}
            ),
            event_sink=None,
            cancel_token=None,
            pre_terminal_state_hook=None,
        )
        assert prepared.graph_continuation is not None
        outcomes = await asyncio.gather(
            runtime.assistant_graph_app.aexecute_time_travel(
                prepared.graph_continuation
            ),
            runtime.assistant_graph_app.aexecute_time_travel(
                prepared.graph_continuation
            ),
            return_exceptions=True,
        )
    finally:
        runtime.close()

    errors = [item for item in outcomes if isinstance(item, GraphExecutionError)]
    assert len(errors) == 1
    assert errors[0].code == "graph_continuation_handle_consumed"


@pytest.mark.parametrize("failure_point", ["consumer", "post_state"])
def test_post_native_failures_carry_structured_phase(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="done",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    request = UserRequest(
        user_id="phase-user", session_id="phase-session", text="phase"
    )
    prepared = runtime._prepare_graph_run(
        request,
        event_sink=None,
        cancel_token=None,
        pre_terminal_state_hook=None,
        run_id=f"phase-{failure_point}",
    )

    def fail_consumer(part):
        raise RuntimeError("consumer failed")

    consumer = fail_consumer if failure_point == "consumer" else None
    if failure_point == "post_state":

        async def fail_getter(*args, **kwargs):
            raise RuntimeError("post-state failed")

        monkeypatch.setattr(
            type(runtime.assistant_graph_app.graph), "aget_state", fail_getter
        )

    async def exercise() -> BaseException:
        try:
            await runtime.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
                part_consumer=consumer,
            )
        except BaseException as exc:
            return exc
        raise AssertionError("failure was not raised")

    try:
        captured = asyncio.run(exercise())
    finally:
        runtime.close()

    assert getattr(captured, "native_started", False) is True


@_async_test
async def test_terminal_native_projection_failure_records_failed_product_terminal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = RunHistoryStore(tmp_path / "projection.jsonl")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="terminal",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        run_history=history,
        checkpointer=InMemorySaver(),
    )
    original = runtime._complete_graph_execution

    def fail_projection(*args, **kwargs):
        original(*args, **kwargs)
        raise GraphExecutionError("projection-failed", "projection failed")

    monkeypatch.setattr(runtime, "_complete_graph_execution", fail_projection)
    try:
        with pytest.raises(GraphExecutionError):
            await runtime.arun_state(
                UserRequest(
                    user_id="projection-user",
                    session_id="projection-session",
                    text="project",
                ),
                run_id="projection-run",
            )
    finally:
        runtime.close()

    assert [record.status for record in history.read_all()] == ["started", "failed"]


@_async_test
async def test_cancelled_time_travel_preflight_has_no_product_lifecycle(
    tmp_path,
) -> None:
    history = RunHistoryStore(tmp_path / "cancelled-continuation.jsonl")
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted",
                    finish_reason="stop",
                    response_text="origin",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        run_history=history,
        checkpointer=InMemorySaver(),
    )
    owner = RequestIdentity.for_user(
        user_id="cancel-cont-user",
        agent_id=runtime.agent_id,
        session_id="cancel-cont-session",
    )
    try:
        await runtime.arun_state(
            UserRequest(
                user_id=owner.user_id, session_id=owner.session_id, text="origin"
            ),
            run_id="cancel-cont-origin",
        )
        checkpoint = (await runtime.alist_history(owner, limit=10))[1]
        baseline = tuple(history.read_all())
        with pytest.raises(AgentRunCancelled):
            await runtime.areplay_state(
                owner,
                GraphReplayRequest(selector={"history_ref": checkpoint.history_ref}),
                run_id="cancel-cont-replay",
                cancel_token=CancelledToken(),
            )
        with pytest.raises(GraphExecutionError) as reused:
            await runtime.areplay_state(
                owner,
                GraphReplayRequest(selector={"history_ref": checkpoint.history_ref}),
                run_id="cancel-cont-replay",
            )
    finally:
        runtime.close()

    assert reused.value.code == "graph_invocation_run_id_reused"
    assert tuple(history.read_all()) == baseline


@_async_test
async def test_resume_rejects_same_owner_with_different_protected_request_facts(
    tmp_path,
) -> None:
    tool = ProbeTool()
    history = RunHistoryStore(tmp_path / "resume-request-mismatch.jsonl")
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="resume-request-action",
                        name=tool.name,
                        arguments={"value": "protected"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted",
                finish_reason="stop",
                response_text="must not execute",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        run_history=history,
        checkpointer=InMemorySaver(),
        allow_interrupt=True,
    )
    original = UserRequest(
        user_id="resume-request-user",
        session_id="resume-request-session",
        text="original protected text",
        image_ids=["original-image"],
        assistant_mode="standard",
        task_execution_mode="foreground",
        response_style="concise",
    )
    try:
        waiting = await runtime.arun_state(
            original,
            run_id="resume-request-origin",
            interrupt_request=AssistantInterruptRequest(
                kind="approval",
                prompt="approve protected request",
                action_ref="resume-request-action",
                allowed_resume_kinds=("approve", "reject"),
            ),
        )
        provider_calls = len(adapter.requests)
        baseline_history = tuple(history.read_all())
        mismatched = original.model_copy(update={"text": "different protected text"})
        with pytest.raises(GraphExecutionError) as captured:
            await runtime.aresume_state(
                mismatched,
                resume=AssistantApproveResume(action_ref="resume-request-action"),
                run_id="resume-request-mismatch",
            )
        with pytest.raises(GraphExecutionError) as reused:
            await runtime.aresume_state(
                original,
                resume=AssistantApproveResume(action_ref="resume-request-action"),
                run_id="resume-request-mismatch",
            )
    finally:
        runtime.close()

    assert waiting.status == "waiting_user"
    assert captured.value.code == "graph_resume_request_mismatch"
    assert reused.value.code == "graph_invocation_run_id_reused"
    assert len(adapter.requests) == provider_calls
    assert tuple(history.read_all()) == baseline_history
