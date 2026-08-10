from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import ToolResult
from evals.release_review.contracts import ReleaseScenario
from evals.release_review.decision_backend import ScenarioExecutionBackend
from evals.release_review.experiment import (
    ReleaseExperimentSettings,
    run_release_experiment,
)
from evals.release_review.loader import scenario_hash
from evals.release_review.staging import (
    CleanupResult,
    PreparedStagingResource,
    StagingResourceManager,
)
from evals.release_review.sync_dataset import RELEASE_REVIEW_DATASET
from tests.core.support import sealed_registry


def _scenario(phase: str) -> ReleaseScenario:
    payload = {
        "id": f"{phase}_probe",
        "phase": phase,
        "capability": "probe",
        "risk": "high",
        "request": f"run {phase}",
        "tool_contract": {
            "required": ["probe_tool"],
            "allowed": [],
            "forbidden": [],
        },
        "state_assertions": [{"path": "status", "equals": "completed"}],
    }
    if phase == "decision":
        payload["fixtures"] = {
            "probe_tool": [{"success": True, "data": {"source": "fixture"}}]
        }
    else:
        payload["staging"] = {
            "resource_profile": "amap_readonly",
            "cleanup": "skipped",
        }
    return ReleaseScenario.model_validate(payload)


class StagingAdapter:
    def prepare(self, *, namespace, scenario):
        return PreparedStagingResource(runtime_metadata={"tenant": namespace})

    def cleanup(self, *, namespace, resource_refs):
        raise AssertionError("readonly cleanup must be skipped")


class FakeTraceStore:
    def list_by_run(self, run_id: str):
        return [
            {"canonical_event": "tool.finished", "tool_name": "probe_tool"},
            {"canonical_event": "response.delivered"},
        ]


class FakeRuntime:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.registry = sealed_registry()
        self.trace_store = FakeTraceStore()
        self.closed = False

    def run_state(self, request: UserRequest) -> AgentState:
        state = AgentState.from_request(request)
        call = state.add_tool_call("probe_tool", {"value": "sentinel"})
        if self.backend is None:
            result = ToolResult(
                tool_name="probe_tool", success=True, data={"source": "staging"}
            )
        else:
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
        self.closed = True
        return True


class FakeDataset:
    def __init__(self, items) -> None:
        self.items = items
        self.kwargs = None
        self.outputs = []
        self.evaluations = []

    def run_experiment(self, **kwargs):
        self.kwargs = kwargs
        for index, item in enumerate(self.items):
            output = kwargs["task"](item=item)
            self.outputs.append(output)
            evaluations = kwargs["evaluators"][0](
                output=output,
                metadata=item.metadata,
                expected_output=item.expected_output,
                input=item.input,
            )
            self.evaluations.extend(evaluations)
        return SimpleNamespace(
            run_name=kwargs["run_name"] or "generated-run",
            dataset_run_id="dataset-run-sentinel",
            dataset_run_url="https://langfuse.invalid/run",
            item_results=[SimpleNamespace(trace_id=f"trace-{index}") for index, _ in enumerate(self.items)],
        )


class FakeClient:
    def __init__(self, dataset: FakeDataset) -> None:
        self.dataset = dataset

    def get_dataset(self, name: str):
        assert name == RELEASE_REVIEW_DATASET
        return self.dataset


def _item(scenario: ReleaseScenario):
    return SimpleNamespace(
        id=f"{RELEASE_REVIEW_DATASET}__{scenario.id}__r1",
        input={"scenario_id": scenario.id, "request": scenario.request},
        expected_output={},
        metadata={
            "scenario_id": scenario.id,
            "scenario_hash": scenario_hash(scenario),
            "phase": scenario.phase,
            "repetition": 1,
        },
        status="ACTIVE",
    )


def test_native_experiment_uses_phase_specific_execution_and_one_evaluator() -> None:
    scenarios = (_scenario("decision"), _scenario("staging"))
    dataset = FakeDataset([_item(scenario) for scenario in scenarios])
    factory_calls = []

    def runtime_factory(scenario, backend, runtime_metadata):
        factory_calls.append((scenario.phase, backend, runtime_metadata))
        return FakeRuntime(backend)

    settings = ReleaseExperimentSettings(
        release_id="release-1",
        model="model-sentinel",
        git_commit="git-sentinel",
        catalog_generation="catalog-sentinel",
        evaluator_version="evaluator-sentinel",
        runtime_factory=runtime_factory,
        staging_resources=StagingResourceManager({"amap_readonly": StagingAdapter()}),
        run_name="run-sentinel",
    )

    result = run_release_experiment(FakeClient(dataset), scenarios, settings)

    assert isinstance(factory_calls[0][1], ScenarioExecutionBackend)
    assert factory_calls[1][1] is None
    assert factory_calls[1][2]["release_review"]["resource_profile"] == "amap_readonly"
    assert dataset.kwargs["max_concurrency"] == 4
    assert dataset.kwargs["metadata"] == {
        "evaluation_mode": "release_review",
        "release_id": "release-1",
        "model": "model-sentinel",
        "git_commit": "git-sentinel",
        "catalog_generation": "catalog-sentinel",
        "evaluator_version": "evaluator-sentinel",
    }
    assert {evaluation.name for evaluation in dataset.evaluations} == {
        "assistant_agent.quality.task_conformance"
    }
    assert result.run_name == "run-sentinel"
    assert result.dataset_run_id == "dataset-run-sentinel"
    assert result.cleanup_results["staging_probe:r1"].status == "skipped"


def test_native_task_rejects_dataset_hash_drift() -> None:
    scenario = _scenario("decision")
    item = _item(scenario)
    item.metadata["scenario_hash"] = "stale"
    dataset = FakeDataset([item])
    settings = ReleaseExperimentSettings(
        release_id="release-1",
        model="model",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        runtime_factory=lambda scenario, backend, metadata: FakeRuntime(backend),
    )

    with pytest.raises(RuntimeError, match="scenario hash mismatch"):
        run_release_experiment(FakeClient(dataset), [scenario], settings)


def test_staging_cleanup_runs_when_runtime_fails() -> None:
    class FailingRuntime(FakeRuntime):
        def run_state(self, request):
            raise RuntimeError("provider unavailable")

    class CleanupAdapter(StagingAdapter):
        def __init__(self):
            self.cleaned = 0

        def cleanup(self, *, namespace, resource_refs):
            self.cleaned += 1
            return CleanupResult(status="succeeded", resource_refs=resource_refs)

    scenario = _scenario("staging").model_copy(
        update={
            "staging": _scenario("staging").staging.model_copy(
                update={"cleanup": "required"}
            )
        }
    )
    adapter = CleanupAdapter()
    dataset = FakeDataset([_item(scenario)])
    settings = ReleaseExperimentSettings(
        release_id="release-1",
        model="model",
        git_commit="git",
        catalog_generation="catalog",
        evaluator_version="evaluator",
        runtime_factory=lambda scenario, backend, metadata: FailingRuntime(backend),
        staging_resources=StagingResourceManager({"amap_readonly": adapter}),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_release_experiment(FakeClient(dataset), [scenario], settings)
    assert adapter.cleaned == 1
