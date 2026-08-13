from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from functools import wraps
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from assistant_agent.runtime.assistant_graph_app import (
    AssistantTurnGraphApp,
    GraphExecutionError,
    GraphStreamPart,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_time_travel import GraphReplayRequest
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import ToolResult
from tests.core.support import (
    ProbeInput,
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


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-replay",
        session_id="session-replay",
        text="replay sentinel",
    )


def _prepare(runtime: AgentGraphRuntime, *, run_id: str):
    return runtime._prepare_graph_run(  # noqa: SLF001 - native App boundary TDD.
        _request(),
        event_sink=None,
        cancel_token=None,
        pre_terminal_state_hook=None,
        run_id=run_id,
    )


async def _selector_for_continuation(runtime: AgentGraphRuntime, continuation: str):
    app = runtime.assistant_graph_app
    identity = _prepare(runtime, run_id="run-history-inspect").identity
    for summary in await app.alist_history(identity, limit=100):
        snapshot = await app._resolve_history_snapshot(  # noqa: SLF001
            identity,
            summary.selector
            if hasattr(summary, "selector")
            else GraphReplayRequest(
                selector={"history_ref": summary.history_ref}
            ).selector,
        )
        if snapshot.values["continuation"] == continuation:
            return summary
    raise AssertionError(f"No replay-safe {continuation!r} checkpoint was produced")


def test_graph_replay_request_is_selector_only() -> None:
    """Adding native config/state fields would expose internal checkpoint identity."""

    request = GraphReplayRequest(
        selector={"history_ref": "ghr_" + "a" * 32},
    )

    assert request.model_dump(mode="json") == {
        "selector": {"history_ref": "ghr_" + "a" * 32}
    }
    with pytest.raises(ValidationError):
        GraphReplayRequest.model_validate(
            {
                "selector": {"history_ref": "ghr_" + "a" * 32},
                "checkpoint_id": "native-id",
            }
        )


def test_public_astream_cannot_accept_a_native_checkpoint_config() -> None:
    """A public config override would bypass the opaque replay selector boundary."""

    assert (
        "runnable_config"
        not in inspect.signature(AssistantTurnGraphApp.astream).parameters
    )


@_async_test
async def test_replay_uses_historical_config_and_unified_stream_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing current config or copied state would miss the selected native branch."""

    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="original-answer",
            )
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-original")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "compose_response")
        selected_snapshot = await runtime.assistant_graph_app._resolve_history_snapshot(  # noqa: SLF001
            original.identity,
            GraphReplayRequest(selector={"history_ref": selected.history_ref}).selector,
        )
        historical_config = deepcopy(selected_snapshot.config)
        historical_configurable = deepcopy(selected_snapshot.config["configurable"])
        replay = _prepare(runtime, run_id="run-replay")
        replay.state.trace_id = first.final_state["run"]["trace_id"]
        replay_context = replace(
            replay.runtime_context,
            invocation_kind="invoke",
        )
        consumed: list[GraphStreamPart] = []
        native_calls: list[tuple[object, dict[str, Any]]] = []
        graph_type = type(runtime.assistant_graph_app.graph)
        original_astream = graph_type.astream

        async def spy_astream(
            graph,
            input_value,
            config=None,
            **kwargs,
        ) -> AsyncIterator[Any]:
            native_calls.append((input_value, config))
            async for item in original_astream(
                graph,
                input_value,
                config=config,
                **kwargs,
            ):
                yield item

        monkeypatch.setattr(graph_type, "astream", spy_astream)
        replayed = await runtime.assistant_graph_app.areplay(
            identity=replay.identity,
            context=replay_context,
            request=GraphReplayRequest(selector={"history_ref": selected.history_ref}),
            part_consumer=consumed.append,
        )
    finally:
        runtime.close()

    assert len(native_calls) == 1
    native_input, native_config = native_calls[0]
    assert native_input is None
    assert native_config["configurable"] == historical_configurable
    assert selected_snapshot.config == historical_config
    assert native_config["metadata"]["run_id"] == "run-replay"
    assert native_config["tags"]
    assert "callbacks" in native_config
    assert tuple(consumed) == replayed.parts
    assert replayed.checkpoint_config is None
    update_nodes = [
        next(iter(part.data))
        for part in replayed.parts
        if part.type == "updates"
        and not part.namespace
        and isinstance(part.data, dict)
        and len(part.data) == 1
    ]
    assert update_nodes.index("prepare_invocation") < update_nodes.index(
        "compose_response"
    )
    assert replayed.final_state["invocation_kind"] == "replay"
    assert replayed.final_state["invocation_run_id"] == "run-replay"
    assert replayed.final_state["turn_origin_id"] == first.final_state["turn_origin_id"]
    assert (
        replayed.final_state["run"]["trace_id"] == first.final_state["run"]["trace_id"]
    )


@_async_test
async def test_replay_rejects_untrusted_trace_without_mutating_context() -> None:
    """Silently adopting checkpoint trace would mask a cross-trace caller mismatch."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="trace-answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-trace-origin")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "compose_response")
        replay = _prepare(runtime, run_id="run-trace-replay")
        untrusted_trace = replay.state.trace_id

        with pytest.raises(GraphExecutionError) as captured:
            await runtime.assistant_graph_app.areplay(
                identity=replay.identity,
                context=replay.runtime_context,
                request=GraphReplayRequest(
                    selector={"history_ref": selected.history_ref}
                ),
            )
        assert replay.state.trace_id == untrusted_trace
        replay.state.trace_id = first.final_state["run"]["trace_id"]
        with pytest.raises(GraphExecutionError) as reused:
            await runtime.assistant_graph_app.areplay(
                identity=replay.identity,
                context=replace(
                    replay.runtime_context,
                    invocation_token="corrected-competing-token",
                ),
                request=GraphReplayRequest(
                    selector={"history_ref": selected.history_ref}
                ),
            )
    finally:
        runtime.close()

    assert captured.value.code == "graph_replay_identity_mismatch"
    assert reused.value.code == "graph_invocation_run_id_reused"
    assert untrusted_trace != first.final_state["run"]["trace_id"]


@_async_test
async def test_same_selector_and_run_id_has_one_replay_claim() -> None:
    """A second branch with the same run id must fail before native execution."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="claim-answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-claim-origin")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "compose_response")
        request = GraphReplayRequest(selector={"history_ref": selected.history_ref})
        replay = _prepare(runtime, run_id="run-claim-replay")
        replay.state.trace_id = first.final_state["run"]["trace_id"]
        replay_context = replace(replay.runtime_context, invocation_kind="replay")

        await runtime.assistant_graph_app.areplay(
            identity=replay.identity,
            context=replay_context,
            request=request,
        )
        with pytest.raises(GraphExecutionError) as captured:
            await runtime.assistant_graph_app.areplay(
                identity=replay.identity,
                context=replace(replay_context, invocation_token="competing-token"),
                request=request,
            )
    finally:
        runtime.close()

    assert captured.value.code == "graph_invocation_run_id_reused"


@_async_test
async def test_invalid_selector_retains_claim_but_same_token_can_retry() -> None:
    """A failed selector must retain run ownership without blocking its own retry."""

    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="selector-answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-selector-origin")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "compose_response")
        replay = _prepare(runtime, run_id="run-selector-replay")
        replay.state.trace_id = first.final_state["run"]["trace_id"]
        context = replace(replay.runtime_context, invocation_kind="replay")
        invalid = GraphReplayRequest(selector={"history_ref": "ghr_" + "f" * 32})

        with pytest.raises(GraphExecutionError) as missing:
            await runtime.assistant_graph_app.areplay(
                identity=replay.identity,
                context=context,
                request=invalid,
            )
        with pytest.raises(GraphExecutionError) as competing:
            await runtime.assistant_graph_app.areplay(
                identity=replay.identity,
                context=replace(context, invocation_token="different-token"),
                request=GraphReplayRequest(
                    selector={"history_ref": selected.history_ref}
                ),
            )
        retried = await runtime.assistant_graph_app.areplay(
            identity=replay.identity,
            context=context,
            request=GraphReplayRequest(selector={"history_ref": selected.history_ref}),
        )
    finally:
        runtime.close()

    assert missing.value.code == "graph_checkpoint_selector_not_found"
    assert competing.value.code == "graph_invocation_run_id_reused"
    assert retried.final_state["invocation_run_id"] == "run-selector-replay"


class _CountingWriteTool(ProbeTool):
    name = "replay_write_probe"
    category = "write"

    def __init__(self) -> None:
        self.invocations = 0

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
            model_observation={"summary": "write committed", "outcome": "success"},
        )


@_async_test
async def test_replay_write_tool_reuses_operation_barrier(tmp_path) -> None:
    """Replaying a pre-tool checkpoint must not repeat a committed write."""

    tool = _CountingWriteTool()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-replay-write",
                        name=tool.name,
                        arguments={"value": "write-sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="original-write-answer",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="replayed-write-answer",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        registry=sealed_registry(tool),
        config=offline_config(),
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
        tool_operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
    )
    original = _prepare(runtime, run_id="run-write-origin")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "execute_tool")
        replay = _prepare(runtime, run_id="run-write-replay")
        replay.state.trace_id = first.final_state["run"]["trace_id"]
        replayed = await runtime.assistant_graph_app.areplay(
            identity=replay.identity,
            context=replace(replay.runtime_context, invocation_kind="replay"),
            request=GraphReplayRequest(selector={"history_ref": selected.history_ref}),
        )
    finally:
        runtime.close()

    assert tool.invocations == 1
    assert replayed.final_state["invocation_run_id"] == "run-write-replay"
    assert first.final_state["run"]["tool_results"][0]["status"] == "succeeded"
    assert replayed.final_state["run"]["tool_results"][0]["status"] == "failed"
    assert replayed.final_state["run"]["tool_results"][0]["error_summary"] == (
        "tool_operation_outcome_unknown"
    )
