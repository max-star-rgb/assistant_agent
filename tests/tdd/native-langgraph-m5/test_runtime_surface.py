"""Temporary RED/GREEN guards for the M5 Runtime retirement surface."""

from __future__ import annotations

import importlib
import inspect

import pytest

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.store import InMemoryWorkflowStore, WorkflowStore


def _public_methods(cls: type[object]) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_runtime_public_surface_is_intentional_with_drain_gate() -> None:
    """Catch reintroduction of unused Runtime facade methods after M5 cleanup."""

    stable_surface = {
        "initialize_session_memory",
        "run_state",
        "arun_state",
        "astream_state",
        "aresume_state",
        "areplay_state",
        "afork_state",
        "drain_memory_ingestions",
        "run_task_quantum",
        "close",
    }

    assert _public_methods(AgentGraphRuntime) == stable_surface


def test_legacy_workflow_execution_modules_and_api_lifecycle_are_removed() -> None:
    """A closed retirement gate must leave StateGraph as the only executor."""

    from assistant_agent.api import app as app_module
    from assistant_agent.api import routes_workflows

    for module_name in (
        "assistant_agent.workflows.worker",
        "assistant_agent.workflows.runtime",
        "assistant_agent.workflows.execution",
        "assistant_agent.workflows.legacy_drain_host",
        "assistant_agent.workflows.planning",
        "assistant_agent.workflows.progress",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)

    assert not hasattr(app_module, "start_durable_workflow_worker")
    assert not hasattr(app_module, "shutdown_durable_workflow_worker")
    assert not hasattr(routes_workflows, "get_legacy_drain_host")


def test_legacy_scheduler_claim_and_lease_surface_is_removed() -> None:
    """No store or config surface may re-enable the retired scheduler."""

    retired_methods = {"claim_ready_work_item", "renew_work_item_lease"}
    for owner in (WorkflowStore, InMemoryWorkflowStore, SQLiteWorkflowStore):
        assert retired_methods.isdisjoint(dir(owner))
    config = ProviderConfig()
    assert not hasattr(config, "durable_workflow_worker_enabled")
    assert not hasattr(config, "durable_workflow_lease_seconds")
    assert not hasattr(config, "durable_workflow_poll_seconds")


def test_tool_capability_mapping_replaces_legacy_module_without_alias() -> None:
    """Catch a stale legacy import path or behavior drift during the rename."""

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assistant_agent.runtime.legacy_tool_mapping")

    mapping = importlib.import_module("assistant_agent.runtime.tool_capability_mapping")
    assert mapping.canonical_tool_for_capability("video_understanding") == (
        "media_inspect"
    )
    assert mapping.canonical_capability_for_tool("media_inspect") == (
        "image_understanding"
    )
    assert mapping.canonical_action_for_capability("web_fetch") == "fetch_web"
    assert mapping.canonical_capability_for_action("read_url") == "web_fetch"
    assert mapping.canonical_tool_for_capability("unknown") is None


def test_recovery_and_planning_modules_keep_their_real_consumers() -> None:
    """Catch accidental deletion of non-legacy runtime governance modules."""

    durable_models = importlib.import_module(
        "assistant_agent.automation.durable_tasks.models"
    )
    durable_service = importlib.import_module(
        "assistant_agent.automation.durable_tasks.service"
    )
    hotel_price_watch = importlib.import_module(
        "assistant_agent.automation.durable_tasks.hotel_price_watch"
    )
    langsmith_native = importlib.import_module(
        "assistant_agent.observability.langsmith_native"
    )
    plan_validator = importlib.import_module("assistant_agent.runtime.plan_validator")
    planning_models = importlib.import_module("assistant_agent.runtime.planning_models")
    recovery = importlib.import_module("assistant_agent.runtime.recovery")
    tool_executor = importlib.import_module("assistant_agent.runtime.tool_executor")

    assert durable_models.TaskPlan is planning_models.TaskPlan
    assert durable_service.PlanValidator is plan_validator.PlanValidator
    assert hotel_price_watch.TaskStep is planning_models.TaskStep
    assert tool_executor.RecoveryPolicy is recovery.RecoveryPolicy
    assert langsmith_native._tool_error_code("provider_timeout: timed out") == (
        "provider_timeout"
    )
