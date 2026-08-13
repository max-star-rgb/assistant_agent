from __future__ import annotations

import asyncio

import pytest

from assistant_agent.runtime.assistant_graph_app import GraphStreamPart
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import ProbeTool, offline_config, sealed_registry


class _StreamingToolTrajectoryAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.call_count = 0

    def chat(self, request):
        self.call_count += 1
        if self.call_count == 1:
            if request.stream_callback is not None:
                request.stream_callback(
                    "before-tool",
                    {"provider": self.provider, "model": self.model},
                )
            return ChatResult(
                provider=self.provider,
                model=self.model,
                finish_reason="tool_calls",
                response_text="before-tool",
                tool_calls=[
                    NativeToolCall(
                        id="provider-call-1",
                        name=ProbeTool.name,
                        arguments={"value": "tool-input"},
                    )
                ],
            )
        if request.stream_callback is not None:
            request.stream_callback(
                "after-tool",
                {"provider": self.provider, "model": self.model},
            )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="after-tool",
        )


class _ProjectorGuardSink(ListEventSink):
    """Reject any public event that did not pass through ProductEventProjector."""

    def emit(self, event) -> None:
        try:
            from assistant_agent.runtime.product_event_projector import (
                current_product_fact_id,
            )
        except ModuleNotFoundError:
            pytest.fail("ProductEventProjector has not been implemented")
        assert current_product_fact_id() is not None, (
            f"{event.type} bypassed ProductEventProjector"
        )
        super().emit(event)


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-product-fact",
        session_id="session-product-fact",
        text="run the probe",
    )


def _runtime() -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        chat_adapter=_StreamingToolTrajectoryAdapter(),
        session_store=InMemorySessionStore(),
    )


def test_real_compiled_v2_stream_carries_strict_node_llm_and_tool_facts_once() -> None:
    """Replacing native custom facts with node-side AgentEvent emission must fail."""

    try:
        from assistant_agent.runtime.product_event_projector import (
            ProductEventProjector,
            validate_runtime_product_fact,
        )
    except ModuleNotFoundError:
        pytest.fail("ProductEventProjector has not been implemented")

    async def exercise() -> None:
        runtime = _runtime()
        sink = ListEventSink()
        projector = ProductEventProjector(event_sink=sink, dedupe_capacity=32)
        prepared = runtime._prepare_graph_run(  # noqa: SLF001 - Graph API TDD.
            _request(),
            event_sink=None,
            cancel_token=None,
            trace_context=None,
            export_trace_context=None,
            pre_terminal_state_hook=None,
            run_id="run-product-stream",
        )
        try:
            parts = [
                part
                async for part in runtime.assistant_graph_app.astream(
                    prepared.initial_state,
                    identity=prepared.identity,
                    context=prepared.runtime_context,
                )
            ]
            custom_parts = [part for part in parts if part.type == "custom"]
            facts = [validate_runtime_product_fact(part.data) for part in custom_parts]
            kinds = [fact.kind for fact in facts]

            assert "text_delta" in kinds
            assert "product_progress" in kinds
            assert "tool_started" in kinds
            assert "tool_terminal" in kinds
            fact_ids = [fact.fact_id for fact in facts]
            assert len(fact_ids) == len(set(fact_ids))

            for part in parts:
                projector.project_part(part)
            projected_count = len(sink.events)
            assert projected_count == len(facts)

            # The same occurrence can arrive through the direct/root path and
            # custom stream path; fact_id is the shared exactly-once boundary.
            for fact, part in zip(facts, custom_parts, strict=True):
                assert projector.project_fact(fact) is None
                assert projector.project_part(part) is None
            assert len(sink.events) == projected_count
        finally:
            runtime.close()

    asyncio.run(exercise())


def test_project_part_ignores_all_non_custom_graph_stream_modes() -> None:
    """Guessing product progress from updates/tasks/checkpoints/state must fail."""

    try:
        from assistant_agent.runtime.product_event_projector import (
            ProductEventProjector,
            RunStartedProductFact,
        )
    except ModuleNotFoundError:
        pytest.fail("ProductEventProjector has not been implemented")

    sink = ListEventSink()
    projector = ProductEventProjector(event_sink=sink)
    fact = RunStartedProductFact(
        fact_id="fact-run-started",
        session_id="session-product-fact",
        run_id="run-product-fact",
        user_id="user-product-fact",
        agent_id="agent-product-fact",
        trace_id="trace-product-fact",
    )
    disguised = fact.model_dump(mode="json")

    for stream_type in ("values", "updates", "messages", "tasks", "checkpoints"):
        assert projector.project_part(
            GraphStreamPart(
                type=stream_type,
                namespace=("assistant:task-id",),
                data=disguised,
            )
        ) is None
    assert sink.events == []


def test_runtime_public_events_have_one_projector_owner_and_no_fake_graph_lifecycle() -> None:
    """Any Runtime/node/LLM/Tool direct EventSink call must fail this mutation guard."""

    runtime = _runtime()
    sink = _ProjectorGuardSink()
    try:
        state = asyncio.run(
            runtime.arun_state(
                _request(),
                event_sink=sink,
                run_id="run-projector-owner",
            )
        )
        event_types = [event.type for event in sink.events]

        assert state.status == "completed"
        assert event_types[0] == "task_started"
        assert event_types[-1] == "final_response"
        assert "response_delta" in event_types
        assert "tool_started" in event_types
        assert "tool_finished" in event_types
        assert "graph_node_started" not in event_types
        assert "graph_node_finished" not in event_types
    finally:
        runtime.close()


def test_runtime_product_fact_union_rejects_unknown_fields() -> None:
    """Weak dict parsing that accepts undeclared product payload must fail."""

    try:
        from assistant_agent.runtime.product_event_projector import (
            RuntimeProductFactValidationError,
            validate_runtime_product_fact,
        )
    except ModuleNotFoundError:
        pytest.fail("ProductEventProjector has not been implemented")

    with pytest.raises(RuntimeProductFactValidationError):
        validate_runtime_product_fact(
            {
                "schema_version": "runtime_product_fact_v1",
                "kind": "text_delta",
                "fact_id": "fact-text-delta",
                "session_id": "session-product-fact",
                "run_id": "run-product-fact",
                "text": "safe-text",
                "source": "assistant",
                "payload": {},
                "checkpoint": {"forbidden": True},
            }
        )


@pytest.mark.parametrize(
    "nested_payload",
    [
        {"safe": {"checkpoint": {"forbidden": True}}},
        {"safe": {"tasks": ["forbidden"]}},
        {"safe": {"ns": ["assistant", "task"]}},
        {"safe": {"state": {"messages": []}}},
        {"safe": object()},
        {"safe": "x" * 262_145},
        {"safe": 2**63},
        {"safe": -(2**63) - 1},
    ],
)
def test_runtime_product_fact_union_rejects_nested_graph_data(
    nested_payload,
) -> None:
    """Nested graph internals and non-JSON objects must not escape custom facts."""

    from assistant_agent.runtime.product_event_projector import (
        RuntimeProductFactValidationError,
        validate_runtime_product_fact,
    )

    with pytest.raises(RuntimeProductFactValidationError):
        validate_runtime_product_fact(
            {
                "schema_version": "runtime_product_fact_v1",
                "kind": "text_delta",
                "fact_id": "fact-nested-rejected",
                "session_id": "session-product-fact",
                "run_id": "run-product-fact",
                "text": "safe-text",
                "source": "assistant",
                "payload": nested_payload,
            }
        )


def test_validated_nested_json_can_always_be_projected() -> None:
    """Accepted scalar boundaries must remain digestible and projectable."""

    from assistant_agent.runtime.product_event_projector import (
        ProductEventProjector,
        validate_runtime_product_fact,
    )

    sink = ListEventSink()
    projector = ProductEventProjector(event_sink=sink)
    fact = validate_runtime_product_fact(
        {
            "schema_version": "runtime_product_fact_v1",
            "kind": "text_delta",
            "fact_id": "fact-bounded-json",
            "session_id": "session-product-fact",
            "run_id": "run-product-fact",
            "text": "safe-text",
            "source": "assistant",
            "payload": {
                "safe_text": "x" * 262_144,
                "min_int": -(2**63),
                "max_int": 2**63 - 1,
            },
        }
    )

    projected = projector.project_fact(fact)

    assert projected is not None
    assert len(sink.events) == 1
