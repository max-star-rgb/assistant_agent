from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from functools import wraps
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from assistant_agent.runtime.assistant_graph_app import (
    GraphExecutionError,
    GraphStreamPart,
)
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_time_travel import (
    GraphForkPatch,
    GraphForkRequest,
)
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


def _request(text: str, *, response_style: str | None = None) -> UserRequest:
    return UserRequest(
        user_id="user-fork",
        session_id="session-fork",
        text=text,
        response_style=response_style,
    )


def _prepare(runtime: AgentGraphRuntime, *, run_id: str, text: str):
    return runtime._prepare_graph_run(  # noqa: SLF001 - native App boundary TDD.
        _request(text),
        event_sink=None,
        cancel_token=None,
        pre_terminal_state_hook=None,
        run_id=run_id,
    )


async def _selector_for_continuation(runtime: AgentGraphRuntime, continuation: str):
    app = runtime.assistant_graph_app
    identity = _prepare(
        runtime,
        run_id="run-fork-history-inspect",
        text="original request",
    ).identity
    for summary in await app.alist_history(identity, limit=100):
        snapshot = await app._resolve_history_snapshot(  # noqa: SLF001
            identity,
            GraphForkRequest(
                selector={"history_ref": summary.history_ref},
                patch={},
            ).selector,
        )
        if snapshot.values["continuation"] == continuation:
            return summary
    raise AssertionError(f"No fork-safe {continuation!r} checkpoint was produced")


class _CountingWriteTool(ProbeTool):
    name = "fork_write_probe"
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


def _assert_no_native_stream_payload(value: Any) -> None:
    forbidden = {
        "checkpoint_id",
        "checkpoint_ns",
        "config",
        "configurable",
        "tasks",
        "interrupt_id",
    }
    if isinstance(value, dict):
        assert not (forbidden & value.keys())
        for item in value.values():
            _assert_no_native_stream_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_native_stream_payload(item)


def test_graph_fork_request_only_accepts_selector_and_allowlisted_product_patch() -> None:
    request = GraphForkRequest(
        selector={"history_ref": "ghr_" + "a" * 32},
        patch={"request_text": "branch request", "response_style": "concise"},
    )

    assert request.model_dump(mode="json") == {
        "selector": {"history_ref": "ghr_" + "a" * 32},
        "patch": {"request_text": "branch request", "response_style": "concise"},
    }
    forbidden = (
        "run_id",
        "trace_id",
        "user_id",
        "session_id",
        "agent_id",
        "profile",
        "catalog",
        "capability_refs",
        "tool_results",
        "pending_tool_calls",
        "operation_scope_id",
        "checkpoint_id",
        "config",
    )
    for field in forbidden:
        with pytest.raises(ValidationError):
            GraphForkRequest.model_validate(
                {
                    "selector": {"history_ref": "ghr_" + "a" * 32},
                    "patch": {field: "tampered"},
                }
            )


def test_graph_fork_patch_rejects_unbounded_or_invalid_product_values() -> None:
    with pytest.raises(ValidationError):
        GraphForkPatch(request_text="x" * 32_001)
    with pytest.raises(ValidationError):
        GraphForkPatch(response_style="admin")


@_async_test
async def test_fork_uses_exact_historical_config_public_update_and_returned_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    tool = _CountingWriteTool()
    adapter = ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="provider-fork-write",
                        name=tool.name,
                        arguments={"value": "write-sentinel"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="original answer",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="branch answer",
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
    original = _prepare(
        runtime,
        run_id="run-fork-origin",
        text="original request",
    )
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "assistant")
        selected_snapshot = await runtime.assistant_graph_app._resolve_history_snapshot(  # noqa: SLF001
            original.identity,
            GraphForkRequest(
                selector={"history_ref": selected.history_ref},
                patch={},
            ).selector,
        )
        historical_config = deepcopy(selected_snapshot.config)
        fork = _prepare(runtime, run_id="run-fork-branch", text="branch request")
        fork.state.trace_id = first.final_state["run"]["trace_id"]
        consumed: list[GraphStreamPart] = []
        update_calls: list[tuple[Any, Any, Any, Any]] = []
        returned_configs: list[dict[str, Any]] = []
        stream_calls: list[tuple[Any, dict[str, Any]]] = []
        graph_type = type(runtime.assistant_graph_app.graph)
        original_update = graph_type.aupdate_state
        original_astream = graph_type.astream

        async def spy_update(
            graph,
            config,
            values,
            as_node=None,
            task_id=None,
        ):
            update_calls.append((config, values, as_node, task_id))
            returned = await original_update(
                graph,
                config,
                values,
                as_node=as_node,
                task_id=task_id,
            )
            returned_configs.append(returned)
            return returned

        async def spy_astream(
            graph,
            input_value,
            config=None,
            **kwargs,
        ) -> AsyncIterator[Any]:
            stream_calls.append((input_value, config))
            yield {
                "type": "custom",
                "ns": (),
                "data": {
                    "schema_version": "runtime_product_fact_v1",
                    "fact_id": "pf.product_progress.native-leak-probe",
                    "session_id": "session-fork",
                    "run_id": "run-fork-branch",
                    "occurred_at": "2026-08-13T00:00:00Z",
                    "kind": "product_progress",
                    "event_type": "progress_message",
                    "tool_name": None,
                    "output_ref": None,
                    "text": "safe progress",
                    "error": None,
                    "payload": {"nested": {"checkpoint_id": "native-secret"}},
                },
            }
            async for item in original_astream(
                graph,
                input_value,
                config=config,
                **kwargs,
            ):
                yield item

        monkeypatch.setattr(graph_type, "aupdate_state", spy_update)
        monkeypatch.setattr(graph_type, "astream", spy_astream)
        branched = await runtime.assistant_graph_app.afork(
            identity=fork.identity,
            context=fork.runtime_context,
            request=GraphForkRequest(
                selector={"history_ref": selected.history_ref},
                patch={"request_text": "branch request"},
            ),
            part_consumer=consumed.append,
        )
    finally:
        runtime.close()

    assert len(update_calls) == 1
    update_config, update_values, as_node, task_id = update_calls[0]
    assert update_config == historical_config
    assert selected_snapshot.config == historical_config
    assert as_node == "time_travel_anchor"
    assert task_id is None
    assert update_values["request"]["text"] == "branch request"
    assert update_values["run"]["run_id"] == "run-fork-origin"
    assert update_values["invocation_run_id"] == "run-fork-origin"
    returned_snapshot = await runtime.assistant_graph_app.graph.aget_state(
        returned_configs[0]
    )
    assert tuple(returned_snapshot.next) == ("prepare_invocation",)
    assert len(stream_calls) == 1
    stream_input, stream_config = stream_calls[0]
    assert stream_input is None
    assert stream_config["configurable"] == returned_configs[0]["configurable"]
    assert tuple(consumed) == branched.parts
    assert branched.checkpoint_config is None
    assert {part.type for part in branched.parts} <= {"updates", "custom"}
    _assert_no_native_stream_payload(
        [{"type": part.type, "namespace": part.namespace, "data": part.data} for part in branched.parts]
    )
    update_nodes = [
        next(iter(part.data))
        for part in branched.parts
        if part.type == "updates"
        and not part.namespace
        and isinstance(part.data, dict)
        and len(part.data) == 1
    ]
    assert update_nodes[:2] == ["prepare_invocation", "assistant"]
    assert branched.final_state["request"]["text"] == "branch request"
    assert branched.final_state["run"]["run_id"] == "run-fork-branch"
    assert branched.final_state["invocation_run_id"] == "run-fork-branch"
    assert branched.final_state["invocation_kind"] == "fork"
    assert branched.final_state["turn_origin_id"] == "run-fork-origin"
    assert branched.final_state["final_response"]["message"] == "branch answer"
    assert tool.invocations == 1


@_async_test
async def test_fork_claims_before_selector_preflight_and_same_run_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="origin answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-fork-claim-origin", text="original request")
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        fork = _prepare(runtime, run_id="run-fork-conflict", text="branch request")
        fork.state.trace_id = first.final_state["run"]["trace_id"]
        entered_resolve = asyncio.Event()
        release_resolve = asyncio.Event()
        original_resolve = runtime.assistant_graph_app._resolve_history_snapshot  # noqa: SLF001

        async def blocked_resolve(identity, selector):
            entered_resolve.set()
            await release_resolve.wait()
            return await original_resolve(identity, selector)

        monkeypatch.setattr(
            runtime.assistant_graph_app,
            "_resolve_history_snapshot",
            blocked_resolve,
        )
        request = GraphForkRequest(
            selector={"history_ref": "ghr_" + "f" * 32},
            patch={"request_text": "branch request"},
        )
        first_task = asyncio.create_task(
            runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=fork.runtime_context,
                request=request,
            )
        )
        await entered_resolve.wait()
        with pytest.raises(GraphExecutionError) as conflicting:
            await runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=replace(
                    fork.runtime_context,
                    invocation_token="competing-fork-token",
                ),
                request=request,
            )
        release_resolve.set()
        with pytest.raises(GraphExecutionError) as missing:
            await first_task
    finally:
        runtime.close()

    assert conflicting.value.code == "graph_invocation_run_id_reused"
    assert missing.value.code == "graph_checkpoint_selector_not_found"


@_async_test
async def test_fork_starts_native_claim_before_update_and_blocks_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="origin answer",
                ),
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="branch answer",
                ),
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-fork-active-origin", text="original request")
    release_update = asyncio.Event()
    update_entered = asyncio.Event()
    update_calls = 0
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "compose_response")
        fork = _prepare(runtime, run_id="run-fork-active", text="branch request")
        fork.state.trace_id = first.final_state["run"]["trace_id"]
        graph_type = type(runtime.assistant_graph_app.graph)
        original_update = graph_type.aupdate_state

        async def blocked_update(graph, *args, **kwargs):
            nonlocal update_calls
            update_calls += 1
            update_entered.set()
            await release_update.wait()
            return await original_update(graph, *args, **kwargs)

        monkeypatch.setattr(graph_type, "aupdate_state", blocked_update)
        request = GraphForkRequest(
            selector={"history_ref": selected.history_ref},
            patch={"request_text": "branch request"},
        )
        active = asyncio.create_task(
            runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=fork.runtime_context,
                request=request,
            )
        )
        await update_entered.wait()
        with pytest.raises(GraphExecutionError) as duplicate:
            await runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=fork.runtime_context,
                request=request,
            )
        with pytest.raises(GraphExecutionError) as deleting:
            await runtime.assistant_graph_app.adelete_thread(
                agent_id=fork.state.agent_id,
                user_id=fork.state.user_id,
                session_id=fork.state.session_id,
                invocation_claim_store=runtime.graph_invocation_claim_store,
            )
        release_update.set()
        await active
    finally:
        release_update.set()
        runtime.close()

    assert duplicate.value.code == "graph_invocation_run_id_reused"
    assert deleting.value.code == "graph_thread_active"
    assert update_calls == 1


@_async_test
async def test_fork_tampered_owner_or_trace_fails_before_native_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=ScriptedChatAdapter(
            [
                ChatResult(
                    provider="scripted",
                    model="scripted-model",
                    finish_reason="stop",
                    response_text="origin answer",
                )
            ]
        ),
        session_store=InMemorySessionStore(),
        checkpointer=InMemorySaver(),
    )
    original = _prepare(runtime, run_id="run-fork-tamper-origin", text="original request")
    update_calls = 0
    try:
        first = await runtime.assistant_graph_app.arun(
            original.initial_state,
            identity=original.identity,
            context=original.runtime_context,
        )
        selected = await _selector_for_continuation(runtime, "compose_response")
        fork = _prepare(runtime, run_id="run-fork-tamper", text="branch request")
        assert fork.state.trace_id != first.final_state["run"]["trace_id"]
        graph_type = type(runtime.assistant_graph_app.graph)
        original_update = graph_type.aupdate_state

        async def spy_update(graph, *args, **kwargs):
            nonlocal update_calls
            update_calls += 1
            return await original_update(graph, *args, **kwargs)

        monkeypatch.setattr(graph_type, "aupdate_state", spy_update)
        with pytest.raises(GraphExecutionError) as captured:
            await runtime.assistant_graph_app.afork(
                identity=fork.identity,
                context=fork.runtime_context,
                request=GraphForkRequest(
                    selector={"history_ref": selected.history_ref},
                    patch={"request_text": "branch request"},
                ),
            )
        assert update_calls == 0
        fork.state.trace_id = first.final_state["run"]["trace_id"]
        retried = await runtime.assistant_graph_app.afork(
            identity=fork.identity,
            context=fork.runtime_context,
            request=GraphForkRequest(
                selector={"history_ref": selected.history_ref},
                patch={"request_text": "branch request"},
            ),
        )
    finally:
        runtime.close()

    assert captured.value.code == "graph_fork_identity_mismatch"
    assert update_calls == 1
    assert retried.final_state["invocation_run_id"] == "run-fork-tamper"
