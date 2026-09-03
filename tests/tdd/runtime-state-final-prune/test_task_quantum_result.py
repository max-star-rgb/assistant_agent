from dataclasses import fields

from assistant_agent.automation.durable_tasks.models import TaskCheckpoint
from assistant_agent.automation.durable_tasks.worker import TaskQuantumResult


def test_task_quantum_result_only_carries_durable_outputs() -> None:
    result = TaskQuantumResult(
        checkpoint=TaskCheckpoint(kind="completed", summary="done")
    )

    assert [field.name for field in fields(result)] == ["checkpoint", "binding"]
