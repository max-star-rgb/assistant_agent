from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.agent import loader


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
