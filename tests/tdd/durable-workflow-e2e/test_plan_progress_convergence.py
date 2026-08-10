from __future__ import annotations

from assistant_agent.automation.durable_tasks.progress import (
    project_durable_task_progress,
)
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.store import InMemoryTaskStore
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.planning_models import TaskPlan, TaskStep
from tests.core.support import ProbeTool, sealed_registry


def test_legacy_durable_task_projects_the_shared_user_visible_plan_progress() -> None:
    service = DurableTaskService(
        store=InMemoryTaskStore(),
        registry=sealed_registry(),
    )
    bundle = service.submit_plan(
        identity=RequestIdentity.for_user(
            user_id="user-sentinel",
            session_id="session-sentinel",
        ),
        ingress_run_id="run-sentinel",
        plan=TaskPlan(
            goal="goal-sentinel",
            steps=[
                TaskStep(
                    step_id="research-hermes",
                    display_title="正在检索并核实 Hermes\n工程资料",
                    action="收集 Hermes 官方文档和一手工程资料。",
                    tool_name=ProbeTool.name,
                )
            ],
        ),
        revision_reason="initial",
    )

    assert project_durable_task_progress(bundle) == {
        "state": "working",
        "plan_kind": "durable_task",
        "work_item_id": "research-hermes",
        "work_item_kind": ProbeTool.name,
        "display_title": "正在检索并核实 Hermes 工程资料",
        "completed_items": 0,
        "total_items": 1,
        "attempt_count": 0,
    }
