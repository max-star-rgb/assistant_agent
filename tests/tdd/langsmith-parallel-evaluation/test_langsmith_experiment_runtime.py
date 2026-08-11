from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from assistant_agent.evaluation import langsmith_trace
from assistant_agent.evaluation.experiment_runtime import (
    create_experiment_runtime_host,
)
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.event_publisher import (
    RunStartedFact,
    RuntimeEventPublisher,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState


def _run_tree() -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("11111111-2222-3333-4444-555555555555"),
        trace_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        session_id=None,
        session_name="run-name-12345678",
        reference_example_id=UUID("01234567-89ab-cdef-0123-456789abcdef"),
        dotted_order=(
            "20260811T120000000000Z"
            "11111111-2222-3333-4444-555555555555"
        ),
    )


def test_current_run_tree_becomes_a_valid_runtime_experiment_binding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(langsmith_trace, "_current_run_tree", _run_tree)

    binding = langsmith_trace.current_langsmith_experiment_binding(
        experiment_id="99999999-8888-7777-6666-555555555555",
        project_name="run-name-12345678",
    )

    assert binding is not None
    assert binding.project_id == "99999999-8888-7777-6666-555555555555"
    assert binding.trace_context.trace_id == "aaaaaaaabbbbccccddddeeeeeeeeeeee"
    assert binding.trace_context.parent_span_id == "1111111122223333"
    link = binding.trace_context.experiment_link
    assert link is not None
    assert link.parent_run_id == "11111111-2222-3333-4444-555555555555"
    assert link.reference_example_id == "01234567-89ab-cdef-0123-456789abcdef"
    assert link.parent_dotted_order == _run_tree().dotted_order


def test_experiment_runtime_host_accepts_injected_langsmith_trace_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(langsmith_trace, "_current_run_tree", _run_tree)
    binding = langsmith_trace.current_langsmith_experiment_binding(
        experiment_id="99999999-8888-7777-6666-555555555555",
        project_name="run-name-12345678",
    )
    assert binding is not None
    received = []

    class TraceStore:
        def close(self, *, timeout: float) -> bool:
            return True

    class Runtime:
        trace_store = None

        def run_state(self, request, *, trace_context=None):
            received.append(trace_context)
            return request

        def close(self) -> bool:
            return True

    host = create_experiment_runtime_host(
        lambda store: Runtime(),
        trace_store_factory=TraceStore,
        trace_context_provider=lambda: binding.trace_context,
    )
    try:
        assert host.run_state("request") == "request"
    finally:
        host.close()

    assert received == [binding.trace_context]


def test_runtime_root_projects_official_langsmith_experiment_links(
    monkeypatch,
) -> None:
    monkeypatch.setattr(langsmith_trace, "_current_run_tree", _run_tree)
    binding = langsmith_trace.current_langsmith_experiment_binding(
        experiment_id="99999999-8888-7777-6666-555555555555",
        project_name="run-name-12345678",
    )
    assert binding is not None
    state = AgentState.from_request(
        UserRequest(
            user_id="runtime-regression",
            session_id="runtime-regression-example",
            text="重跑问题",
        ),
        run_id="run-1",
        trace_id=binding.trace_context.trace_id,
    )
    store = InMemoryTraceStore()
    RuntimeEventPublisher(event_sink=None, trace_store=store).record_run_started(
        RunStartedFact(
            state=state,
            parent_span_id=binding.trace_context.parent_span_id,
            execution_engine="langgraph_assistant_loop",
            experiment_trace_link=binding.trace_context.experiment_link,
        )
    )

    root = build_text_otel_span_specs(store.list_by_run("run-1"))[0]

    assert root.attributes["langsmith.trace.id"] == str(_run_tree().trace_id)
    assert root.attributes["langsmith.span.parent_id"] == str(_run_tree().id)
    assert UUID(root.attributes["langsmith.span.id"])
    assert root.attributes["langsmith.span.dotted_order"].startswith(
        _run_tree().dotted_order + "."
    )
    assert root.attributes["langsmith.trace.session_id"] == (
        "99999999-8888-7777-6666-555555555555"
    )
    assert root.attributes["langsmith.reference_example_id"] == str(
        _run_tree().reference_example_id
    )
    assert root.attributes["langsmith.trace.session_name"] == "run-name-12345678"
