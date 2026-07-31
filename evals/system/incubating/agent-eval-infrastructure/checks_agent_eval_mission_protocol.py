from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.agent import cli, loader
from evals.agent.contracts import (
    AssertionResult,
    JudgeVerdict,
    RunEvidence,
    TaskJudgeResult,
    TaskSpec,
)
from evals.agent.grading import (
    dimension,
    enforce_tool_outcome_expectations,
    environment_validation,
    grade_task,
    judge_assertion,
    rule_assertion,
    validate_mission_objective_assertions,
)
from evals.agent.loader import AgentEvalCaseSource, load_task
from evals.agent.langfuse_backend import publish_tasks


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def create_dataset(self, **_: Any) -> object:
        return object()

    def create_dataset_item(self, **kwargs: Any) -> object:
        self.items.append(kwargs)
        return object()


def _write_task(root: Path, task_id: str) -> None:
    task_dir = root / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "id": task_id,
                "description": f"{task_id} description",
                "capability": f"{task_id}_capability",
                "request": {
                    "user_id": "eval-user",
                    "session_id": "eval-session",
                    "text": "完成受控任务。",
                },
                "environment": (
                    "evals.agent.tasks.email_empty_result_honesty."
                    "environment:EmailEmptyResultEnvironment"
                ),
                "grader": (
                    "evals.agent.tasks.email_empty_result_honesty.grader:grade"
                ),
                "tags": ["offline"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_loader_discovers_tasks_and_missions_with_source_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_root = tmp_path / "tasks"
    missions_root = tmp_path / "missions"
    _write_task(tasks_root, "basic_case")
    _write_task(missions_root, "mission_case")
    monkeypatch.setattr(loader, "TASKS_ROOT", tasks_root)
    monkeypatch.setattr(loader, "MISSIONS_ROOT", missions_root)

    sources = loader.list_case_sources()

    assert [(item.task_id, item.level) for item in sources] == [
        ("basic_case", "task"),
        ("mission_case", "mission"),
    ]
    assert loader.list_task_ids() == ["basic_case", "mission_case"]
    assert loader.load_task("mission_case").id == "mission_case"


def test_publish_mission_keeps_langfuse_dataset_item_thin() -> None:
    mission = TaskSpec(
        id="mission_case",
        description="Mission dataset contract.",
        capability="mission_capability",
        request={
            "user_id": "eval-user",
            "session_id": "eval-session",
            "text": "完成受控任务。",
        },
        environment="example.mission:Environment",
        grader="example.mission:grade",
        tags=["offline", "mission"],
    )
    client = _FakeLangfuseClient()

    publish_tasks(client, [mission])

    item = client.items[0]
    assert item["input"] == {
        "task_id": mission.id,
        "request": mission.request.model_dump(mode="json"),
    }
    assert item["metadata"] == {
        "task_id": mission.id,
        "capability": mission.capability,
        "tags": mission.tags,
    }
    assert "case_level" not in item["metadata"]
    assert "environment" not in item["metadata"]
    assert "objective_state" not in item["metadata"]
    assert "grader" not in item["metadata"]


def test_loader_rejects_duplicate_ids_across_case_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_root = tmp_path / "tasks"
    missions_root = tmp_path / "missions"
    _write_task(tasks_root, "duplicate_case")
    _write_task(missions_root, "duplicate_case")
    monkeypatch.setattr(loader, "TASKS_ROOT", tasks_root)
    monkeypatch.setattr(loader, "MISSIONS_ROOT", missions_root)

    with pytest.raises(ValueError, match="Duplicate.*duplicate_case"):
        loader.list_case_sources()


def test_calibration_path_uses_discovered_mission_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_root = tmp_path / "tasks"
    missions_root = tmp_path / "missions"
    _write_task(missions_root, "mission_case")
    calibration_file = missions_root / "mission_case" / "calibration.json"
    calibration_file.write_text(
        json.dumps({"schema_version": "agent_eval_calibration_v3", "fixtures": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "TASKS_ROOT", tasks_root)
    monkeypatch.setattr(loader, "MISSIONS_ROOT", missions_root)

    assert loader.calibration_path("mission_case") == calibration_file


def test_inspect_reports_case_level_and_relative_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = load_task("email_empty_result_honesty")
    environment = loader.load_entrypoint(task.environment)()
    environment.objective_state_assertions = lambda evidence: {
        "synthetic_state": rule_assertion(
            True,
            f"task_id={evidence.task_id}",
            label="合成终态有效",
        )
    }
    monkeypatch.setattr(
        cli,
        "load_case_source",
        lambda _: AgentEvalCaseSource(
            task_id=task.id,
            level="mission",
            directory=Path("/tmp/mission"),
            relative_path="evals/agent/missions/mission_case",
        ),
    )
    monkeypatch.setattr(
        cli,
        "load_entrypoint",
        lambda _: lambda: environment,
    )

    payload = cli._inspect_task(task)

    assert payload["case_source"] == {
        "level": "mission",
        "path": "evals/agent/missions/mission_case",
    }
    assert payload["mission_objective_rule"]["required"] is True


def _passed_task_judge_result() -> TaskJudgeResult:
    def passed_dimension(criterion_id: str, label: str):
        return dimension(
            {
                criterion_id: judge_assertion(
                    JudgeVerdict(passed=True, reason="通过。"),
                    criterion_id=criterion_id,
                    label=label,
                )
            }
        )

    return TaskJudgeResult(
        tool_semantics=passed_dimension(
            "tool_semantics",
            "工具语义 Judge 通过",
        ),
        grounding=passed_dimension(
            "grounding",
            "Grounding Judge 通过",
        ),
        response_quality=passed_dimension(
            "response_quality",
            "回答质量 Judge 通过",
        ),
    )


def test_mission_objective_rules_join_tool_execution_dimension() -> None:
    evidence = RunEvidence(
        task_id="mission_case",
        run_id="run-1",
        trace_id="a" * 32,
        terminal_status="completed",
        available_tools=[],
    )

    result = enforce_tool_outcome_expectations(
        _passed_task_judge_result(),
        evidence=evidence,
        expectations=[],
        objective_assertions={
            "single_event": rule_assertion(
                False,
                "added=0",
                label="新增唯一暂定事件",
            )
        },
    )

    assert result.dimensions.tool_execution.passed is False
    assert set(result.dimensions.tool_execution.assertions) == {
        "outcome_matches_environment",
        "mission_state.single_event",
    }


@pytest.mark.parametrize(
    "objective_assertions",
    [
        {},
        {
            "judged_state": judge_assertion(
                JudgeVerdict(passed=True, reason="通过。"),
                criterion_id="grounding",
                label="错误使用 Judge 的状态检查",
            )
        },
    ],
)
def test_mission_objective_rules_must_be_nonempty_rules(
    objective_assertions: dict[str, AssertionResult],
) -> None:
    with pytest.raises(RuntimeError, match="Mission objective"):
        validate_mission_objective_assertions(objective_assertions)


class _BasicEnvironment:
    def validate(self):
        return environment_validation(
            {
                "configured": rule_assertion(
                    True,
                    "local fixture",
                    label="本地环境可用",
                )
            }
        )

    def tool_outcome_expectations(self, available_tools: list[str]):
        assert available_tools == []
        return []


def test_grade_task_requires_mission_objective_rules_but_not_task_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = load_task("email_empty_result_honesty")
    evidence = RunEvidence(
        task_id=task.id,
        run_id="run-1",
        trace_id="a" * 32,
        terminal_status="completed",
    )
    environment = _BasicEnvironment()

    def load_fixture_entrypoint(value: str):
        if value == task.environment:
            return lambda: environment
        assert value == task.grader
        return lambda evidence, judge: _passed_task_judge_result()

    monkeypatch.setattr(loader, "load_entrypoint", load_fixture_entrypoint)
    monkeypatch.setattr(
        loader,
        "load_case_source",
        lambda _: AgentEvalCaseSource(
            task_id=task.id,
            level="mission",
            directory=Path("/tmp/mission"),
            relative_path="evals/agent/missions/mission_case",
        ),
    )

    with pytest.raises(RuntimeError, match="Mission .*objective_state_assertions"):
        grade_task(task=task, evidence=evidence, judge=object())

    monkeypatch.setattr(
        loader,
        "load_case_source",
        lambda _: AgentEvalCaseSource(
            task_id=task.id,
            level="task",
            directory=Path("/tmp/task"),
            relative_path="evals/agent/tasks/basic_case",
        ),
    )

    result = grade_task(task=task, evidence=evidence, judge=object())

    assert result.dimensions.tool_execution.passed is True
    assert set(result.dimensions.tool_execution.assertions) == {
        "outcome_matches_environment"
    }
