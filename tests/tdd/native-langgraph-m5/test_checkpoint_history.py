from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import StateSnapshot
from pydantic import ValidationError

from assistant_agent.runtime.assistant_graph_app import (
    AssistantTurnGraphApp,
    GraphExecutionError,
    GraphExecutionIdentity,
)
from assistant_agent.runtime.assistant_graph_state import (
    ASSISTANT_GRAPH_VERSION,
    ASSISTANT_STATE_SCHEMA_VERSION,
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.graph_time_travel import (
    GraphCheckpointSelector,
    graph_history_ref,
)
from assistant_agent.runtime.requests import UserRequest


_NATIVE_KEYS = {
    "checkpoint_id",
    "checkpoint_ns",
    "config",
    "values",
    "tasks",
    "state",
}


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@dataclass
class _HistoryGraph:
    snapshots: tuple[StateSnapshot, ...]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def aget_state_history(
        self,
        config: Mapping[str, Any],
        *,
        before: Mapping[str, Any] | None = None,
        limit: int,
    ) -> AsyncIterator[StateSnapshot]:
        self.calls.append(
            {"config": deepcopy(dict(config)), "before": deepcopy(before), "limit": limit}
        )
        start = 0
        if before is not None:
            for index, snapshot in enumerate(self.snapshots):
                if snapshot.config == before:
                    start = index + 1
                    break
            else:
                return
        for snapshot in self.snapshots[start : start + limit]:
            yield snapshot


def _identity(
    *,
    user_id: str = "history-user",
    session_id: str = "history-session",
    run_id: str = "history-inspect",
) -> GraphExecutionIdentity:
    return GraphExecutionIdentity.for_assistant_turn(
        agent_id="history-agent",
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
    )


def _state(
    *,
    user_id: str = "history-user",
    session_id: str = "history-session",
    status: str = "running",
) -> dict[str, Any]:
    state = assistant_turn_state_from_request(
        UserRequest(
            user_id=user_id,
            session_id=session_id,
            text="history probe",
        ),
        run_id="historical-run",
        trace_id="historical-trace",
        agent_id="history-agent",
    )
    state["run"]["status"] = status
    return state


def _snapshot(
    checkpoint_id: str,
    *,
    identity: GraphExecutionIdentity | None = None,
    next_nodes: tuple[str, ...] = ("prepare_invocation",),
    state: Mapping[str, Any] | None = None,
    interrupted: bool = False,
) -> StateSnapshot:
    owner = identity or _identity()
    config = {
        "configurable": {
            "thread_id": owner.thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        }
    }
    return StateSnapshot(
        values=deepcopy(dict(state or _state())),
        next=next_nodes,
        config=config,
        metadata={"source": "loop", "step": 1},
        created_at="2026-08-13T01:02:03+00:00",
        parent_config=None,
        tasks=(),
        interrupts=(SimpleNamespace(id=f"interrupt-{checkpoint_id}"),)
        if interrupted
        else (),
    )


def _app(*snapshots: StateSnapshot) -> tuple[AssistantTurnGraphApp, _HistoryGraph]:
    graph = _HistoryGraph(tuple(snapshots))
    return AssistantTurnGraphApp.from_compiled_graph(graph), graph


class _FailingHistoryGraph:
    async def aget_state_history(self, *args, **kwargs):
        raise RuntimeError("backend-secret")
        yield  # pragma: no cover - keeps this an async iterator.


class _UnboundedHistoryGraph:
    def __init__(self) -> None:
        self.yielded = 0

    async def aget_state_history(self, *args, **kwargs):
        for index in range(10_000):
            self.yielded += 1
            yield _snapshot(f"ignored-limit-{index}", next_nodes=("assistant",))


def _assert_no_native_keys(value: object) -> None:
    if isinstance(value, dict):
        assert not (_NATIVE_KEYS & value.keys())
        for child in value.values():
            _assert_no_native_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_native_keys(child)


@_async_test
async def test_history_returns_opaque_selector_without_native_ids() -> None:
    eligible = _snapshot("native-checkpoint-secret", interrupted=True)
    app, _ = _app(eligible)

    items = await app.alist_history(_identity(), limit=10)

    assert len(items) == 1
    payload = items[0].model_dump(mode="json")
    assert payload == {
        "history_ref": payload["history_ref"],
        "created_at": "2026-08-13T01:02:03Z",
        "status": "running",
        "next_nodes": ["prepare_invocation"],
        "has_interrupt": True,
        "graph_version": ASSISTANT_GRAPH_VERSION,
        "state_schema_version": ASSISTANT_STATE_SCHEMA_VERSION,
    }
    assert payload["history_ref"].startswith("ghr_")
    assert "native-checkpoint-secret" not in payload["history_ref"]
    _assert_no_native_keys(payload)


@_async_test
async def test_history_selects_only_prepare_invocation_reentry_checkpoints() -> None:
    app, _ = _app(
        _snapshot("terminal", next_nodes=(), state=_state(status="completed")),
        _snapshot("provider", next_nodes=("assistant",)),
        _snapshot("tool", next_nodes=("execute_tool",)),
        _snapshot("compose", next_nodes=("compose_response",)),
        _snapshot("input", next_nodes=("await_input",)),
        _snapshot("eligible"),
    )

    items = await app.alist_history(_identity(), limit=10)

    assert [item.next_nodes for item in items] == [("prepare_invocation",)]


@_async_test
async def test_history_paginates_native_history_to_fill_safe_limit() -> None:
    snapshots = tuple(
        _snapshot(f"unsafe-{index}", next_nodes=("assistant",))
        for index in range(105)
    ) + (_snapshot("eligible-after-first-page"),)
    app, graph = _app(*snapshots)

    items = await app.alist_history(_identity(), limit=1)

    assert len(items) == 1
    assert [call["limit"] for call in graph.calls] == [100, 100]
    assert graph.calls[0]["before"] is None
    assert graph.calls[1]["before"] == snapshots[99].config


@_async_test
async def test_history_scan_is_bounded_when_no_safe_checkpoint_exists() -> None:
    snapshots = tuple(
        _snapshot(f"unsafe-{index}", next_nodes=("assistant",))
        for index in range(550)
    )
    app, graph = _app(*snapshots)

    assert await app.alist_history(_identity(), limit=1) == ()
    assert sum(call["limit"] for call in graph.calls) == 500
    assert len(graph.calls) == 5


@_async_test
async def test_native_history_failure_is_structured_and_does_not_leak() -> None:
    app = AssistantTurnGraphApp.from_compiled_graph(_FailingHistoryGraph())

    with pytest.raises(GraphExecutionError) as captured:
        await app.alist_history(_identity(), limit=1)

    assert captured.value.code == "graph_checkpoint_history_unavailable"
    assert "backend-secret" not in captured.value.message


@_async_test
async def test_backend_cannot_exceed_native_page_limit() -> None:
    graph = _UnboundedHistoryGraph()
    app = AssistantTurnGraphApp.from_compiled_graph(graph)

    with pytest.raises(GraphExecutionError) as captured:
        await app.alist_history(_identity(), limit=1)

    assert captured.value.code == "graph_checkpoint_history_invalid"
    assert graph.yielded == 101


@_async_test
async def test_created_checkpoint_projects_to_running_status() -> None:
    app, _ = _app(_snapshot("created", state=_state(status="created")))

    items = await app.alist_history(_identity(), limit=1)

    assert items[0].status == "running"


@_async_test
async def test_before_uses_resolved_native_config_without_exposing_it() -> None:
    newest = _snapshot("eligible-newest")
    unsafe = _snapshot("unsafe-middle", next_nodes=("assistant",))
    older = _snapshot("eligible-older")
    app, graph = _app(newest, unsafe, older)
    first_page = await app.alist_history(_identity(), limit=1)
    graph.calls.clear()

    second_page = await app.alist_history(
        _identity(),
        limit=1,
        before=GraphCheckpointSelector(history_ref=first_page[0].history_ref),
    )

    assert len(second_page) == 1
    assert second_page[0].history_ref == graph_history_ref(
        thread_id=_identity().thread_id,
        snapshot_config=older.config,
    )
    assert any(call["before"] == newest.config for call in graph.calls)
    _assert_no_native_keys(second_page[0].model_dump(mode="json"))


@_async_test
@pytest.mark.parametrize("kind", ["unknown", "expired", "cross_thread"])
async def test_selector_not_found_fails_closed(kind: str) -> None:
    current = _snapshot("current")
    app, _ = _app(current)
    if kind == "cross_thread":
        other_identity = _identity(user_id="other-user", session_id="other-session")
        selector = GraphCheckpointSelector(
            history_ref=graph_history_ref(
                thread_id=other_identity.thread_id,
                snapshot_config=_snapshot(
                    "other-thread", identity=other_identity
                ).config,
            )
        )
    else:
        selector = GraphCheckpointSelector(
            history_ref=graph_history_ref(
                thread_id=_identity().thread_id,
                snapshot_config={
                    "configurable": {
                        "thread_id": _identity().thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": kind,
                    }
                },
            )
        )

    with pytest.raises(GraphExecutionError) as captured:
        await app._resolve_history_snapshot(_identity(), selector)

    assert captured.value.code == "graph_checkpoint_selector_not_found"
    assert selector.history_ref not in captured.value.message


@_async_test
@pytest.mark.parametrize("invalid_kind", ["owner", "thread", "schema"])
async def test_history_rejects_invalid_owner_thread_or_schema(invalid_kind: str) -> None:
    invalid_state = _state()
    identity = _identity()
    snapshot_identity = identity
    if invalid_kind == "owner":
        invalid_state["run"]["agent_id"] = "other-agent"
    elif invalid_kind == "thread":
        snapshot_identity = _identity(user_id="other-user")
    else:
        invalid_state["state_schema_version"] = 999
    app, _ = _app(
        _snapshot("invalid", identity=snapshot_identity, state=invalid_state)
    )

    with pytest.raises(GraphExecutionError) as captured:
        await app.alist_history(identity, limit=1)

    assert captured.value.code == "graph_checkpoint_history_invalid"


def test_selector_contract_is_strict_and_opaque() -> None:
    selector = GraphCheckpointSelector(history_ref="ghr_" + "a" * 32)
    assert selector.model_dump(mode="json") == {"history_ref": "ghr_" + "a" * 32}
    with pytest.raises(ValidationError):
        GraphCheckpointSelector.model_validate(
            {"history_ref": "native-checkpoint-id", "config": {}}
        )


@_async_test
async def test_current_assistant_graph_exposes_task_2_reentry_gate() -> None:
    app = AssistantTurnGraphApp(checkpointer=InMemorySaver())

    assert "prepare_invocation" in app.graph.nodes
    assert "time_travel_anchor" in app.graph.nodes
    assert await app.alist_history(_identity(), limit=10) == ()
