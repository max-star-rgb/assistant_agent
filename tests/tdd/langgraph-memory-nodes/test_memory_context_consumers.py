from __future__ import annotations

from dataclasses import dataclass

from assistant_agent.context.builder import build_assistant_context_pack
from assistant_agent.runtime.assistant_graph_state import (
    MemoryContext,
    MemoryContextItem,
    assistant_loop_state_from_turn_state,
    assistant_turn_state_from_request,
    memory_context_texts,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.input_binding import (
    RuntimeInputBinding,
    bind_runtime_tool_input,
)


@dataclass(frozen=True)
class _MemoryBoundTool:
    name: str = "memory-consumer-probe"
    runtime_input_bindings = (
        RuntimeInputBinding(
            field="summaries",
            source="memory_context",
            key="summaries",
        ),
        RuntimeInputBinding(
            field="text",
            source="memory_context",
            key="text",
        ),
    )


def _request() -> UserRequest:
    return UserRequest(user_id="user-1", session_id="session-1", text="current")


def _context(*, backend_id: str, memory_id: str, relevance: float) -> MemoryContext:
    return MemoryContext(
        backend_id=backend_id,
        status="ready",
        snapshot_id=f"snapshot-{backend_id}",
        items=(
            MemoryContextItem(
                memory_id=memory_id,
                text="正文一",
                source=backend_id,
                relevance=relevance,
            ),
            MemoryContextItem(
                memory_id=f"{memory_id}-2",
                text="正文二",
                source=backend_id,
                relevance=1.0 - relevance,
            ),
        ),
    )


def test_checkpoint_hydration_projects_only_ordered_memory_text() -> None:
    runtime_state = AgentState.from_request(
        _request(),
        run_id="run-1",
        trace_id="trace-1",
    )
    checkpoint = assistant_turn_state_from_request(
        _request(),
        run_id="run-1",
        trace_id="trace-1",
    )
    checkpoint["memory_context"] = _context(
        backend_id="mem0",
        memory_id="opaque-id",
        relevance=0.9,
    ).model_dump(mode="json")
    checkpoint["run_phase"] = "act"
    checkpoint = validate_assistant_turn_state(checkpoint)

    hydrated = assistant_loop_state_from_turn_state(
        checkpoint,
        runtime_state=runtime_state,
    )

    assert hydrated["state"].memory_texts == ("正文一", "正文二")


def test_context_builder_ignores_memory_observability_metadata() -> None:
    first = AgentState.from_request(_request())
    first.memory_texts = memory_context_texts(
        _context(backend_id="mem0", memory_id="mem0-id", relevance=0.99)
    )
    second = AgentState.from_request(_request())
    second.memory_texts = memory_context_texts(
        _context(backend_id="langmem", memory_id="langmem-id", relevance=0.01)
    )

    first_pack = build_assistant_context_pack(
        state=first,
        iteration=0,
        max_iterations=1,
    )
    second_pack = build_assistant_context_pack(
        state=second,
        iteration=0,
        max_iterations=1,
    )

    assert first_pack.memory_summaries == ["正文一", "正文二"]
    assert first_pack.memory_text == "正文一\n正文二"
    assert second_pack.memory_summaries == first_pack.memory_summaries
    assert second_pack.memory_text == first_pack.memory_text
    assert first_pack.memory_source_ids == []
    assert second_pack.memory_source_ids == []


def test_tool_binding_consumes_only_frozen_memory_text() -> None:
    state = AgentState.from_request(_request())
    state.memory_texts = ("正文一", "正文二")

    bound = bind_runtime_tool_input(
        _MemoryBoundTool(),
        {},
        state=state,
        step_id="step-1",
        context_metadata={},
    )

    assert bound == {
        "summaries": ["正文一", "正文二"],
        "text": "正文一\n正文二",
    }
