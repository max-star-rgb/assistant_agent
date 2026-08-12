from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.runtime.state import AgentState
from evals.langsmith_runtime_regression import experiment


EXAMPLE_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")
TRACE_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _example() -> SimpleNamespace:
    return SimpleNamespace(
        id=EXAMPLE_ID,
        inputs={
            "role": "user",
            "content": "重跑问题",
            "chars": 4,
            "truncated": False,
        },
        outputs={
            "role": "assistant",
            "content": "原始失败回答",
            "chars": 6,
            "truncated": False,
            "terminal_status": "completed",
        },
        metadata={"active": True},
    )


class _AsyncRuntime:
    trace_store = SimpleNamespace(list_by_run=lambda _: [])

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests = []
        self.closed = False

    async def arun_state(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("graph failed")
        state = AgentState.from_request(
            request,
            run_id="run-regression",
            trace_id=TRACE_ID.hex,
        )
        state.status = "completed"
        state.response = AgentResponse(message="修复后的回答")
        return state

    def close(self):
        self.closed = True
        return True


class _AsyncResult:
    experiment_id = UUID("99999999-8888-7777-6666-555555555555")
    experiment_name = "native"
    url = "https://smith.invalid/experiment"

    def __init__(self, example: SimpleNamespace) -> None:
        self.rows = [
            {
                "example": example,
                "run": SimpleNamespace(id=UUID(int=1)),
                "evaluation_results": {"results": []},
            }
        ]

    def __aiter__(self):
        async def rows():
            for row in self.rows:
                yield row

        return rows()

    def get_dataset_id(self):
        return UUID(int=2)


class _AsyncEvaluateClient:
    def __init__(self) -> None:
        self.dataset = SimpleNamespace(id=UUID(int=2), name="dataset")
        self.examples = [_example()]
        self.created_project = None
        self.aevaluate_call = None

    def read_dataset(self, *, dataset_name):
        return self.dataset

    def list_examples(self, *, dataset_id):
        return iter(self.examples)

    def create_project(self, project_name, **kwargs):
        self.created_project = SimpleNamespace(
            id=UUID("99999999-8888-7777-6666-555555555555"),
            name=project_name,
        )
        return self.created_project

    async def aevaluate(self, target, /, **kwargs):
        self.aevaluate_call = kwargs
        await target(self.examples[0].inputs)
        return _AsyncResult(self.examples[0])


def test_dataset_target_awaits_native_graph_under_current_example(
    monkeypatch,
) -> None:
    runtime = _AsyncRuntime()
    client = _AsyncEvaluateClient()
    monkeypatch.setattr(
        experiment,
        "_current_run_tree",
        lambda: SimpleNamespace(
            id=UUID(int=9),
            trace_id=TRACE_ID,
            reference_example_id=EXAMPLE_ID,
        ),
    )

    result = asyncio.run(
        experiment.run_langsmith_runtime_regression_experiment(
            client,
            experiment.LangSmithRuntimeRegressionSettings(
                model="model",
                runtime_factory=lambda: runtime,
                run_name="native",
                git_commit="abc123",
            ),
        )
    )

    assert runtime.requests[0].text == "重跑问题"
    assert runtime.requests[0].metadata["runtime_regression"] == {
        "dataset_item_id": str(EXAMPLE_ID),
        "backend": "langsmith",
    }
    assert runtime.closed is True
    assert result.example_ids == (str(EXAMPLE_ID),)
    assert client.aevaluate_call["experiment"] is client.created_project


def test_dataset_target_closes_runtime_when_native_graph_raises(
    monkeypatch,
) -> None:
    runtime = _AsyncRuntime(fail=True)
    monkeypatch.setattr(
        experiment,
        "_current_run_tree",
        lambda: SimpleNamespace(
            id=UUID(int=9),
            trace_id=TRACE_ID,
            reference_example_id=EXAMPLE_ID,
        ),
    )

    with pytest.raises(RuntimeError, match="graph failed"):
        asyncio.run(
            experiment.run_langsmith_runtime_regression_experiment(
                _AsyncEvaluateClient(),
                experiment.LangSmithRuntimeRegressionSettings(
                    model="model",
                    runtime_factory=lambda: runtime,
                    run_name="native",
                    git_commit="abc123",
                ),
            )
        )

    assert runtime.closed is True


def test_dataset_target_fails_closed_when_runtime_does_not_close(
    monkeypatch,
) -> None:
    class CloseFailureRuntime(_AsyncRuntime):
        def close(self):
            self.closed = True
            return False

    runtime = CloseFailureRuntime()
    monkeypatch.setattr(
        experiment,
        "_current_run_tree",
        lambda: SimpleNamespace(
            id=UUID(int=9),
            trace_id=TRACE_ID,
            reference_example_id=EXAMPLE_ID,
        ),
    )

    with pytest.raises(RuntimeError, match="failed to close"):
        asyncio.run(
            experiment.run_langsmith_runtime_regression_experiment(
                _AsyncEvaluateClient(),
                experiment.LangSmithRuntimeRegressionSettings(
                    model="model",
                    runtime_factory=lambda: runtime,
                    run_name="native",
                    git_commit="abc123",
                ),
            )
        )

    assert runtime.closed is True


def _run(
    run_id: int,
    *,
    name: str,
    parent: int | None,
    run_type: str = "chain",
    example_id: UUID | None = None,
    trace_id: UUID = TRACE_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID(int=run_id),
        parent_run_id=UUID(int=parent) if parent is not None else None,
        name=name,
        run_type=run_type,
        reference_example_id=example_id,
        trace_id=trace_id,
        inputs={"value": "input"},
        outputs={"value": "output"},
    )


def _native_tree() -> list[SimpleNamespace]:
    return [
        _run(1, name="experiment-item-task", parent=None, example_id=EXAMPLE_ID),
        _run(2, name="AssistantTurnGraph", parent=1),
        _run(3, name="assistant", parent=2),
        _run(4, name="llm.chat", parent=3, run_type="llm"),
        _run(5, name="compose_response", parent=2),
    ]


def test_native_tree_audit_accepts_real_graph_parentage() -> None:
    result = experiment.audit_native_graph_tree(
        _native_tree(),
        example_ids=(str(EXAMPLE_ID),),
    )

    assert result.complete is True
    assert result.run_ids == (str(UUID(int=1)),)
    assert result.problems == {}


@pytest.mark.parametrize(
    "mutate,problem",
    [
        (
            lambda runs: [run for run in runs if run.name != "AssistantTurnGraph"],
            "AssistantTurnGraph child count=0",
        ),
        (
            lambda runs: [
                SimpleNamespace(**{**vars(run), "parent_run_id": UUID(int=9)})
                if run.name == "AssistantTurnGraph"
                else run
                for run in runs
            ],
            "AssistantTurnGraph child count=0",
        ),
        (
            lambda runs: [
                SimpleNamespace(**{**vars(run), "parent_run_id": UUID(int=1)})
                if run.name == "llm.chat"
                else run
                for run in runs
            ],
            "missing llm.chat in graph subtree",
        ),
        (
            lambda runs: [
                SimpleNamespace(
                    **{
                        **vars(run),
                        "reference_example_id": UUID(int=7),
                    }
                )
                if run.name == "AssistantTurnGraph"
                else run
                for run in runs
            ],
            "reference example mismatch",
        ),
    ],
)
def test_native_tree_audit_fails_closed_for_shadow_or_mismatched_trees(
    mutate,
    problem,
) -> None:
    result = experiment.audit_native_graph_tree(
        mutate(_native_tree()),
        example_ids=(str(EXAMPLE_ID),),
    )

    assert result.complete is False
    assert problem in result.problems[str(EXAMPLE_ID)]


def test_native_tree_audit_requires_governed_tool_below_execute_tool() -> None:
    valid = _native_tree() + [
        _run(6, name="execute_tool", parent=2),
        _run(7, name="probe_tool", parent=6, run_type="tool"),
    ]
    invalid = [
        SimpleNamespace(**{**vars(run), "parent_run_id": UUID(int=1)})
        if run.run_type == "tool"
        else run
        for run in valid
    ]

    assert experiment.audit_native_graph_tree(
        valid, example_ids=(str(EXAMPLE_ID),)
    ).complete
    result = experiment.audit_native_graph_tree(
        invalid, example_ids=(str(EXAMPLE_ID),)
    )
    assert result.complete is False
    assert "governed tool outside execute_tool subtree" in result.problems[
        str(EXAMPLE_ID)
    ]


def test_native_tree_audit_rejects_extra_llm_sibling_of_graph() -> None:
    result = experiment.audit_native_graph_tree(
        _native_tree() + [_run(8, name="llm.chat", parent=1, run_type="llm")],
        example_ids=(str(EXAMPLE_ID),),
    )

    assert result.complete is False
    assert "llm.chat outside assistant subtree" in result.problems[
        str(EXAMPLE_ID)
    ]


def test_native_tree_audit_rejects_trace_mismatch_inside_graph() -> None:
    runs = [
        SimpleNamespace(**{**vars(run), "trace_id": UUID(int=99)})
        if run.name == "compose_response"
        else run
        for run in _native_tree()
    ]

    result = experiment.audit_native_graph_tree(
        runs,
        example_ids=(str(EXAMPLE_ID),),
    )

    assert result.complete is False
    assert "trace mismatch" in result.problems[str(EXAMPLE_ID)]
