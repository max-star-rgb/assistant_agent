from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from langsmith.utils import LangSmithNotFoundError

from evals.langsmith_workflow_regression.dataset import (
    load_git_workflow_examples,
    sync_workflow_examples,
)
from evals.langsmith_workflow_regression.contracts import WorkflowDatasetExample
from evals.langsmith_workflow_regression.cli import _workflow_trace_id


def test_git_workflow_examples_cover_all_four_cases_and_sync_idempotently() -> None:
    examples = load_git_workflow_examples()
    assert {item.inputs.case_type for item in examples} == {
        "parallel_join",
        "constraint_verifier",
        "minimal_repair",
        "interrupt_resume_equivalence",
    }

    class Client:
        def __init__(self) -> None:
            self.dataset = None
            self.examples = {}

        def read_dataset(self, *, dataset_name):
            if self.dataset is None:
                raise LangSmithNotFoundError("missing")
            return self.dataset

        def create_dataset(self, name, **_kwargs):
            self.dataset = SimpleNamespace(id=UUID(int=1), name=name)
            return self.dataset

        def list_examples(self, *, dataset_id):
            assert dataset_id == UUID(int=1)
            return iter(self.examples.values())

        def create_example(self, *, example_id, **kwargs):
            value = SimpleNamespace(id=example_id, **kwargs)
            self.examples[str(example_id)] = value
            return value

        def update_example(self, example_id, **kwargs):
            current = self.examples[str(example_id)]
            for key, value in kwargs.items():
                setattr(current, key, value)

    client = Client()
    first = sync_workflow_examples(client, examples, git_commit="git-one")
    second = sync_workflow_examples(client, examples, git_commit="git-two")
    assert len(first.active_example_ids) == 4
    assert second.active_example_ids == first.active_example_ids
    assert len(client.examples) == 4
    assert all(item.metadata["git_commit"] == "git-two" for item in client.examples.values())


def test_langsmith_roundtrip_accepts_only_bounded_dataset_split() -> None:
    raw = load_git_workflow_examples()[0].model_dump(mode="json")
    raw["metadata"]["dataset_split"] = ["base"]
    parsed = WorkflowDatasetExample.model_validate(raw)
    assert parsed.metadata.dataset_split == ("base",)

    raw["metadata"]["dataset_split"] = []
    with __import__("pytest").raises(ValueError):
        WorkflowDatasetExample.model_validate(raw)

    raw["metadata"]["dataset_split"] = ["unknown"]
    with __import__("pytest").raises(ValueError):
        WorkflowDatasetExample.model_validate(raw)


def test_workflow_trace_id_is_stable_hex_and_hides_operator_inputs() -> None:
    value = _workflow_trace_id("run-name-secret", "example-secret")
    assert len(value) == 32
    assert set(value) <= set("0123456789abcdef")
    assert value == _workflow_trace_id("run-name-secret", "example-secret")
    assert value != _workflow_trace_id("other-run", "example-secret")
    assert "run-name" not in value
    assert "example" not in value
