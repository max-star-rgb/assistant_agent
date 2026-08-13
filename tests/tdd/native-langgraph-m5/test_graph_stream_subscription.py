from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, get_args

import pytest

from assistant_agent.runtime.assistant_graph_app import (
    ASSISTANT_GRAPH_STREAM_SUBSCRIPTION,
    GraphCheckpointPart,
    GraphCustomPart,
    GraphMessagePart,
    GraphStreamMode,
    GraphStreamSubscription,
    GraphTaskPart,
    GraphUpdatePart,
    GraphValuesPart,
    parse_graph_stream_part,
)
from assistant_agent.runtime.product_event_projector import (
    ProductEventProjector,
    RunStartedProductFact,
    WaitingInputProductFact,
)
from assistant_agent.workflows.durable_graph_app import (
    WORKFLOW_GRAPH_STREAM_SUBSCRIPTION,
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionIdentity,
)


def _async_test(function):
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class _V2StreamProbe:
    def __init__(self, rows: tuple[dict[str, Any], ...]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def astream(self, _input, **kwargs) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(kwargs)
        for row in self.rows:
            yield row


def test_subscription_and_parser_enforce_the_native_v2_allowlist() -> None:
    assert get_args(GraphStreamMode) == (
        "values",
        "updates",
        "messages",
        "custom",
        "tasks",
        "checkpoints",
    )
    assert ASSISTANT_GRAPH_STREAM_SUBSCRIPTION == GraphStreamSubscription(
        modes=get_args(GraphStreamMode),
        include_subgraphs=True,
        durability=None,
    )
    assert WORKFLOW_GRAPH_STREAM_SUBSCRIPTION == GraphStreamSubscription(
        modes=("values", "updates", "custom", "tasks", "checkpoints"),
        include_subgraphs=True,
        durability="sync",
    )

    parsed = tuple(
        parse_graph_stream_part(row)
        for row in (
            {"type": "values", "ns": (), "data": {"status": "running"}},
            {"type": "updates", "ns": (), "data": {"assistant": {}}},
            {
                "type": "messages",
                "ns": (),
                "data": (object(), {"langgraph_node": "assistant"}),
            },
            {
                "type": "custom",
                "ns": ("worker:opaque",),
                "data": "non-product-custom-payload",
            },
            {"type": "tasks", "ns": (), "data": {"id": "internal"}},
            {"type": "checkpoints", "ns": (), "data": {"config": {"internal": True}}},
        )
    )
    assert tuple(type(part) for part in parsed) == (
        GraphValuesPart,
        GraphUpdatePart,
        GraphMessagePart,
        GraphCustomPart,
        GraphTaskPart,
        GraphCheckpointPart,
    )

    for invalid in (
        {"type": "debug", "ns": (), "data": {}},
        {"type": "updates", "ns": "root", "data": {}},
        {"type": "values", "ns": (), "data": object()},
        {"type": "messages", "ns": (), "data": (object(), "metadata")},
        {"type": "tasks", "ns": ("",), "data": {}},
    ):
        with pytest.raises(ValueError):
            parse_graph_stream_part(invalid)

    with pytest.raises(ValueError):
        GraphStreamSubscription(modes=("values", "debug"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GraphStreamSubscription(modes=("values", "values"))
    with pytest.raises(ValueError):
        GraphStreamSubscription(modes=("values",), durability="write")  # type: ignore[arg-type]


@_async_test
async def test_assistant_and_workflow_pass_one_subscription_to_v2_streams() -> None:
    assistant_graph = _V2StreamProbe(
        (
            {"type": "values", "ns": (), "data": {"status": "running"}},
            {"type": "messages", "ns": (), "data": (object(), {})},
        )
    )
    from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
    from assistant_agent.runtime.assistant_graph_app import GraphExecutionIdentity

    assistant = AssistantTurnGraphApp.from_compiled_graph(assistant_graph)
    assistant_parts = [
        part
        async for part in assistant._astream_unclaimed(  # noqa: SLF001 - Graph API contract probe.
            None,
            identity=GraphExecutionIdentity("thread", "run", "agent"),
            context=object(),  # type: ignore[arg-type]
            begin_native=lambda: None,
        )
    ]
    assert [part.type for part in assistant_parts] == ["values", "messages"]
    assistant_call = assistant_graph.calls[0]
    assert assistant_call["version"] == "v2"
    assert (
        tuple(assistant_call["stream_mode"])
        == ASSISTANT_GRAPH_STREAM_SUBSCRIPTION.modes
    )
    assert assistant_call["subgraphs"] is True
    assert "durability" not in assistant_call

    workflow_graph = _V2StreamProbe(
        ({"type": "updates", "ns": ("worker:opaque",), "data": {"worker": {}}},)
    )
    workflow = DurableWorkflowGraphApp(workflow_graph)
    workflow_parts = [
        part
        async for part in workflow.astream(
            {},
            identity=WorkflowGraphExecutionIdentity.for_workflow(
                workflow_id="wf",
                workflow_thread_id="thread",
                run_id="run",
                user_id="user",
                session_id="session",
                agent_id="agent",
            ),
            context=object(),  # type: ignore[arg-type]
        )
    ]
    assert workflow_parts[0].namespace == ("worker:opaque",)
    workflow_call = workflow_graph.calls[0]
    assert workflow_call["version"] == "v2"
    assert (
        tuple(workflow_call["stream_mode"]) == WORKFLOW_GRAPH_STREAM_SUBSCRIPTION.modes
    )
    assert workflow_call["subgraphs"] is True
    assert workflow_call["durability"] == "sync"
    assert "messages" not in workflow_call["stream_mode"]


def test_product_projector_accepts_only_strict_custom_facts_without_native_ids() -> (
    None
):
    projector = ProductEventProjector(event_sink=None)
    fact = RunStartedProductFact(
        fact_id="fact-stream-subscription",
        session_id="session",
        run_id="run",
        user_id="user",
        agent_id="agent",
        trace_id="trace",
    ).model_dump(mode="python")
    assert (
        projector.project_part(
            parse_graph_stream_part({"type": "custom", "ns": (), "data": fact})
        )
        is not None
    )

    for native_key in (
        "checkpoint_id",
        "checkpoint_ns",
        "config",
        "configurable",
        "interrupt_id",
        "task_id",
    ):
        unsafe = dict(fact)
        unsafe["fact_id"] = f"fact-{native_key}"
        unsafe["payload"] = {native_key: "native-secret"}
        unsafe["kind"] = "product_progress"
        unsafe["event_type"] = "progress_message"
        assert (
            projector.project_part(
                parse_graph_stream_part({"type": "custom", "ns": (), "data": unsafe})
            )
            is None
        )

    waiting = WaitingInputProductFact(
        fact_id="fact-waiting",
        session_id="session",
        run_id="run",
        interrupt_kind="input",
        prompt="Provide a safe value.",
        action_ref="action-ref",
        interrupt_id="native-interrupt-secret",
    )
    event = ProductEventProjector(event_sink=None).project_part(
        parse_graph_stream_part(
            {"type": "custom", "ns": (), "data": waiting.model_dump(mode="python")}
        )
    )
    assert event is not None
    assert "interrupt_id" not in event.payload


@_async_test
async def test_stream_parser_fails_before_consumer_and_preserves_backpressure_order() -> (
    None
):
    rows = (
        {"type": "updates", "ns": (), "data": {"first": {}}},
        {"type": "debug", "ns": (), "data": {}},
        {"type": "updates", "ns": (), "data": {"third": {}}},
    )
    graph = _V2StreamProbe(rows)
    app = DurableWorkflowGraphApp(graph)
    consumed: list[str] = []

    with pytest.raises(ValueError):
        async for part in app.astream(
            {},
            identity=WorkflowGraphExecutionIdentity.for_workflow(
                workflow_id="wf",
                workflow_thread_id="thread",
                run_id="run",
                user_id="user",
                session_id="session",
                agent_id="agent",
            ),
            context=object(),  # type: ignore[arg-type]
        ):
            consumed.append(next(iter(part.data)))
    assert consumed == ["first"]
