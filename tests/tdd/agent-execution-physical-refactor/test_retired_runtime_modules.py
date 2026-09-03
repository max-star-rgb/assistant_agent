from __future__ import annotations

import importlib.util
from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "events",
        "generated_artifacts",
        "hook_dispatch",
        "image_to_3d_jobs",
        "planning_models",
        "proactive_messages",
        "recovery",
        "system_prompt_policy",
    ],
)
def test_retired_runtime_module_is_not_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.runtime.{module_name}"

    assert importlib.util.find_spec(qualified_name) is None


@pytest.mark.parametrize("module_name", ["hook_dispatch", "recovery", "trace_store"])
def test_retired_observability_module_is_not_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.observability.{module_name}"

    with pytest.raises(ModuleNotFoundError) as caught:
        import_module(qualified_name)
    assert caught.value.name in {
        "assistant_agent.observability",
        qualified_name,
    }


def test_durable_task_plan_models_are_owned_by_durable_tasks() -> None:
    models = import_module("assistant_agent.automation.durable_tasks.models")

    assert models.TaskPlan.__module__ == models.__name__
    assert models.TaskStep.__module__ == models.__name__


@pytest.mark.parametrize(
    "module_name",
    ["generated_artifacts", "image_to_3d_jobs", "proactive_messages"],
)
def test_media_owned_runtime_contract_is_importable(module_name: str) -> None:
    qualified_name = f"assistant_agent.media.{module_name}"

    assert import_module(qualified_name) is not None
