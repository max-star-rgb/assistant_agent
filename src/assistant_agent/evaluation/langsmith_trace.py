"""Bind the active LangSmith Experiment RunTree to the production Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.observability.trace_context import (
    RuntimeExperimentTraceLink,
    RuntimeTraceContext,
)


@dataclass(frozen=True)
class LangSmithExperimentBinding:
    project_id: str
    trace_context: RuntimeTraceContext


def current_langsmith_experiment_binding() -> LangSmithExperimentBinding | None:
    """Return the active LangSmith target identity, if it is complete."""

    run = _current_run_tree()
    if (
        run is None
        or getattr(run, "id", None) is None
        or getattr(run, "trace_id", None) is None
        or getattr(run, "session_id", None) is None
        or getattr(run, "reference_example_id", None) is None
    ):
        return None
    run_id = run.id
    trace_id = run.trace_id
    experiment_id = run.session_id
    reference_example_id = run.reference_example_id
    return LangSmithExperimentBinding(
        project_id=str(experiment_id),
        trace_context=RuntimeTraceContext(
            trace_id=trace_id.hex,
            parent_span_id=run_id.bytes[:8].hex(),
            experiment_link=RuntimeExperimentTraceLink(
                backend="langsmith",
                trace_id=str(trace_id),
                parent_run_id=str(run_id),
                experiment_id=str(experiment_id),
                reference_example_id=str(reference_example_id),
            ),
        ),
    )


def _current_run_tree() -> Any | None:
    try:
        from langsmith.run_helpers import get_current_run_tree
    except ModuleNotFoundError:
        return None
    return get_current_run_tree()
