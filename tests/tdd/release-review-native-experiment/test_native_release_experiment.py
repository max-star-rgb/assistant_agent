from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import evals.release_review.experiment as experiment_module
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.base import ToolContext
from evals.release_review.contracts import ReleaseScenario
from evals.release_review.decision_backend import ScenarioExecutionBackend
from evals.release_review.experiment import (
    ReleaseExperimentSettings,
    inspect_release_examples,
    run_release_experiment,
)
from evals.release_review.langsmith_backend import GIT_EXAMPLE_OWNER
from evals.release_review.loader import scenario_hash
from tests.core.support import sealed_registry


def _scenario() -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": "decision_probe",
            "phase": "decision",
            "capability": "probe",
            "risk": "high",
            "request": "run decision probe",
            "tool_contract": {"required": ["probe_tool"]},
            "fixtures": {
                "probe_tool": [{"success": True, "data": {"source": "fixture"}}]
            },
            "state_assertions": [{"path": "status", "equals": "completed"}],
        }
    )


def _example(scenario: ReleaseScenario):
    return SimpleNamespace(
        id="example-sentinel",
        inputs={"scenario_id": scenario.id, "request": scenario.request},
        metadata={
            "owner": GIT_EXAMPLE_OWNER,
            "active": True,
            "scenario_id": scenario.id,
            "repetition": 1,
            "scenario_hash": scenario_hash(scenario),
        },
    )


class FakeTraceStore:
    def list_by_run(self, run_id: str):
        return [
            {"canonical_event": "tool.finished", "tool_name": "probe_tool"},
            {"canonical_event": "response.delivered"},
        ]


class FakeRuntime:
    def __init__(self, backend: ScenarioExecutionBackend) -> None:
        self.backend = backend
        self.registry = sealed_registry()
        self.trace_store = FakeTraceStore()

    async def arun_state(self, request: UserRequest) -> AgentState:
        state = AgentState.from_request(request)
        call = state.add_tool_call("probe_tool", {"value": "sentinel"})
        result = self.backend.run(
            self.registry,
            "probe_tool",
            {"value": "sentinel"},
            ToolContext(run_id=state.run_id),
        )
        state.complete_tool_call(call.tool_call_id, result)
        state.set_response(AgentResponse(message="done"))
        return state

    def close(self) -> bool:
        return True


class FakeNative:
    experiment_id = "experiment-sentinel"
    experiment_name = "release-sentinel"
    url = "https://smith.invalid/experiment-sentinel"

    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        self._iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def get_dataset_id(self):
        return "dataset-sentinel"


class FakeClient:
    def __init__(self, example) -> None:
        self.example = example

    def read_dataset(self, *, dataset_name):
        return SimpleNamespace(id="dataset-sentinel")

    def list_examples(self, *, dataset_id):
        return [self.example]

    def create_project(self, name, **kwargs):
        return SimpleNamespace(id="experiment-sentinel", name=name)

    async def aevaluate(self, target, *, evaluators, **kwargs):
        output = await target(self.example.inputs)
        run = SimpleNamespace(id="run-sentinel", outputs=output)
        feedback = evaluators[0](run, self.example)
        assert feedback["key"] == "assistant_agent.quality.task_conformance"
        return FakeNative([SimpleNamespace(example=self.example, run=run)])


def test_native_experiment_executes_actual_runtime_target(monkeypatch) -> None:
    scenario = _scenario()
    example = _example(scenario)
    monkeypatch.setattr(
        experiment_module,
        "_current_run_tree",
        lambda: SimpleNamespace(
            id="task-run-sentinel",
            trace_id="trace-sentinel",
            reference_example_id=example.id,
        ),
    )
    factory_calls = []

    def runtime_factory(selected, backend, metadata):
        factory_calls.append((selected, backend, metadata))
        return FakeRuntime(backend)

    result = asyncio.run(
        run_release_experiment(
            FakeClient(example),
            [scenario],
            ReleaseExperimentSettings(
                release_id="release-1",
                model="model",
                git_commit="git",
                catalog_generation="catalog",
                evaluator_version="evaluator",
                runtime_factory=runtime_factory,
            ),
        )
    )

    assert isinstance(factory_calls[0][1], ScenarioExecutionBackend)
    assert result.experiment_id == "experiment-sentinel"
    assert result.example_ids == (example.id,)
    assert result.run_ids == ("run-sentinel",)


def test_release_examples_reject_git_scenario_hash_drift() -> None:
    scenario = _scenario()
    example = _example(scenario)
    example.metadata["scenario_hash"] = "stale"

    with pytest.raises(RuntimeError, match="scenario hash mismatch"):
        inspect_release_examples(FakeClient(example), [scenario])
