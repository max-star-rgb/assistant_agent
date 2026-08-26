"""Agent Skills integration backed by Deep Agents' native middleware."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, LsResult
from deepagents.middleware import FilesystemMiddleware, SkillsMiddleware
from deepagents.middleware.skills import SkillMetadata


PROJECT_SKILLS_SOURCE = "/"
_PROJECT_SKILLS_SYSTEM_PROMPT = """## Skills

{skills_locations}{skills_load_warnings}

{skills_list}

仅当当前请求明确匹配某项 Skill，且完整指引会影响下一步决策时，才使用 `read_file` 读取对应的
`SKILL.md`。需要辅助文件时，按 `SKILL.md` 给出的绝对路径继续读取。

Skill 提供领域知识，不授予工具，也不等于激活 Tool Profile。遵循 Skill 时直接执行，不向用户介绍 Skill、
文件读取或内部流程。
"""
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def create_project_skills_backend(skills_root: str | Path) -> FilesystemBackend:
    """Create the read source used by native Skill discovery and narrow loaders."""

    return FilesystemBackend(root_dir=Path(skills_root), virtual_mode=True)


def create_project_skills_middleware(
    backend: BackendProtocol,
) -> SkillsMiddleware:
    """Create the upstream middleware that owns Skill discovery and prompting."""

    return SkillsMiddleware(
        backend=backend,
        sources=[(PROJECT_SKILLS_SOURCE, "Project")],
        system_prompt=_PROJECT_SKILLS_SYSTEM_PROMPT,
    )


def create_project_skill_filesystem_middleware(
    backend: BackendProtocol,
) -> FilesystemMiddleware:
    """Expose only upstream ``read_file`` inside the virtual Skill root."""

    return FilesystemMiddleware(
        backend=backend,
        tools=["read_file"],
    )


def native_skill_metadata(state: Mapping[str, Any]) -> tuple[SkillMetadata, ...]:
    """Return validated Agent Skills metadata from middleware-private state."""

    raw = state.get("skills_metadata")
    if not isinstance(raw, list):
        return ()
    result: list[SkillMetadata] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        path = item.get("path")
        description = item.get("description")
        if not all(isinstance(value, str) and value for value in (name, path, description)):
            continue
        if _SKILL_ID_PATTERN.fullmatch(name) is None:
            continue
        skill_path = PurePosixPath(path)
        if skill_path.name != "SKILL.md" or skill_path.parent.name != name:
            continue
        result.append(item)  # type: ignore[arg-type]
    return tuple(result)


def list_skill_reference_ids(
    backend: BackendProtocol,
    metadata: SkillMetadata,
) -> list[str]:
    """Discover flat Markdown references under one native Skill directory."""

    references_dir = PurePosixPath(metadata["path"]).parent / "references"
    listing = backend.ls(str(references_dir))
    entries = listing.entries if isinstance(listing, LsResult) else listing
    reference_ids: list[str] = []
    for entry in entries or []:
        if entry.get("is_dir"):
            continue
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            continue
        path = PurePosixPath(path_value)
        reference_id = path.stem
        if (
            path.parent == references_dir
            and path.suffix.lower() == ".md"
            and _SKILL_ID_PATTERN.fullmatch(reference_id) is not None
        ):
            reference_ids.append(reference_id)
    return sorted(set(reference_ids))


__all__ = [
    "PROJECT_SKILLS_SOURCE",
    "create_project_skill_filesystem_middleware",
    "create_project_skills_backend",
    "create_project_skills_middleware",
    "list_skill_reference_ids",
    "native_skill_metadata",
]
