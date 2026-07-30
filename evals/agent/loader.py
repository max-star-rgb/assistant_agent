"""Load self-contained eval tasks and their Python entrypoints."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evals.agent.contracts import TaskSpec


CaseLevel = Literal["task", "mission"]
TASKS_ROOT = Path(__file__).resolve().parent / "tasks"
MISSIONS_ROOT = Path(__file__).resolve().parent / "missions"
CASE_ROOTS: tuple[tuple[CaseLevel, str], ...] = (
    ("task", "tasks"),
    ("mission", "missions"),
)
SUITES_PATH = Path(__file__).resolve().parent / "suites.json"


@dataclass(frozen=True)
class AgentEvalCaseSource:
    task_id: str
    level: CaseLevel
    directory: Path
    relative_path: str


def list_case_sources() -> list[AgentEvalCaseSource]:
    roots = {
        "task": TASKS_ROOT,
        "mission": MISSIONS_ROOT,
    }
    by_id: dict[str, AgentEvalCaseSource] = {}
    duplicates: set[str] = set()
    for level, relative_root in CASE_ROOTS:
        root = roots[level]
        for path in sorted(root.glob("*/task.json")):
            if not path.is_file():
                continue
            task_id = path.parent.name
            source = AgentEvalCaseSource(
                task_id=task_id,
                level=level,
                directory=path.parent,
                relative_path=f"evals/agent/{relative_root}/{task_id}",
            )
            if task_id in by_id:
                duplicates.add(task_id)
            else:
                by_id[task_id] = source
    if duplicates:
        raise ValueError(
            "Duplicate Agent eval task_id across tasks/missions: "
            + ", ".join(sorted(duplicates))
        )
    return [by_id[task_id] for task_id in sorted(by_id)]


def load_case_source(task_id: str) -> AgentEvalCaseSource:
    by_id = {item.task_id: item for item in list_case_sources()}
    try:
        return by_id[task_id]
    except KeyError as exc:
        raise ValueError(f"Unknown Agent eval task: {task_id}.") from exc


def list_task_ids() -> list[str]:
    return [source.task_id for source in list_case_sources()]


def load_task(task_id: str) -> TaskSpec:
    if task_id not in list_task_ids():
        raise ValueError(f"Unknown Agent eval task: {task_id}.")
    task_path = load_case_source(task_id).directory / "task.json"
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
