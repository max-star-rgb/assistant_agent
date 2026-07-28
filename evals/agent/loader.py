"""Load self-contained eval tasks and their Python entrypoints."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from evals.agent.contracts import TaskSpec


TASKS_ROOT = Path(__file__).resolve().parent / "tasks"
SUITES_PATH = Path(__file__).resolve().parent / "suites.json"


def list_task_ids() -> list[str]:
    return sorted(
        path.parent.name for path in TASKS_ROOT.glob("*/task.json") if path.is_file()
    )


def load_task(task_id: str) -> TaskSpec:
    if task_id not in list_task_ids():
        raise ValueError(f"Unknown Agent eval task: {task_id}.")
    task_path = TASKS_ROOT / task_id / "task.json"
    task = TaskSpec.model_validate_json(task_path.read_text(encoding="utf-8"))
    if task.id != task_id:
        raise ValueError(
            f"Task directory {task_id!r} contains mismatched id {task.id!r}."
        )
    return task


def load_suite(suite_name: str) -> list[str]:
    payload = json.loads(SUITES_PATH.read_text(encoding="utf-8"))
    task_ids = payload.get(suite_name)
    if not isinstance(task_ids, list) or not task_ids:
        raise ValueError(f"Unknown or empty Agent eval suite: {suite_name}.")
    known = set(list_task_ids())
    invalid = [task_id for task_id in task_ids if task_id not in known]
    if invalid:
        raise ValueError(f"Suite {suite_name!r} references unknown tasks: {invalid}.")
    return list(task_ids)


def list_suites() -> list[str]:
    payload = json.loads(SUITES_PATH.read_text(encoding="utf-8"))
    return sorted(payload)


def load_entrypoint(value: str) -> Any:
    module_name, separator, attribute_name = value.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError(f"Entrypoint must use module:attribute syntax: {value!r}.")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute_name)
    except AttributeError as exc:
        raise ValueError(f"Entrypoint does not exist: {value!r}.") from exc
