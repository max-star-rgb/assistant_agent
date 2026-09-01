"""Agent Skills integration backed by Deep Agents' native middleware."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, LsResult
from deepagents.middleware import FilesystemMiddleware, SkillsMiddleware
from deepagents.middleware.skills import SkillMetadata

from assistant_agent.runtime.local_backend import WorkingDirectoryBackend


SOURCE_SKILLS_SOURCE = "/source-skills/"
CWD_SKILLS_SOURCE = "/cwd-skills/"
PROJECT_FILESYSTEM_TOOL_NAMES = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "execute",
)
PROJECT_FILESYSTEM_READ_TOOL_NAMES = ("ls", "read_file", "glob", "grep")
_PROJECT_SKILLS_SYSTEM_PROMPT = """## Skills

{skills_locations}{skills_load_warnings}

{skills_list}

源码 Skill 随 Agent 发布；Working Directory Skill 来自当前 `<cwd>/skills/`，同名时后者优先。

当请求匹配某项 Skill，先使用 `activate_tool_profile` 激活 `filesystem` Tool Profile，再使用 `read_file` 读取对应的 `SKILL.md`。

不向用户介绍 Skill、文件读取或内部流程。
"""
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class _ProjectSkillMetadata(TypedDict):
    name: str
    description: str
    path: str


class _WorkingDirectorySkillsBackend(WorkingDirectoryBackend):
    def __init__(self, source_skills_root: Path) -> None:
        super().__init__("skills", virtual_mode=True)
        self._source_skills_root = source_skills_root.resolve()

    def ls(self, path: str):
        backend = self._backend()
        return (
            backend.ls(path)
            if backend.cwd.is_dir() and backend.cwd != self._source_skills_root
            else LsResult(entries=[])
        )

    async def als(self, path: str):
        backend = self._backend()
        return (
            await backend.als(path)
            if backend.cwd.is_dir() and backend.cwd != self._source_skills_root
            else LsResult(entries=[])
        )


def _project_skills_update(
    update: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if update is None:
        return None
    return {
        **update,
        "skills_metadata": [
            {
                "name": skill["name"],
                "description": skill["description"],
                "path": skill["path"],
            }
            for skill in update["skills_metadata"]
        ],
    }


class _ProjectSkillsMiddleware(SkillsMiddleware):
    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        if not skills:
            return super()._format_skills_list(skills)
        return "\n".join(
            f"- **{skill['name']}**: {skill['description']}\n"
            f"  -> Read `{skill['path']}` for full instructions"
            for skill in skills
        )

    def before_agent(self, state, runtime, config):
        return _project_skills_update(
            super().before_agent(_without_cached_skills(state), runtime, config)
        )

    async def abefore_agent(self, state, runtime, config):
        return _project_skills_update(
            await super().abefore_agent(
                _without_cached_skills(state), runtime, config
            )
        )


def _without_cached_skills(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key not in {"skills_metadata", "skills_load_errors"}
    }


def create_project_skills_backend(
    project_root: str | Path,
    working_backend: BackendProtocol | None = None,
) -> CompositeBackend:
    """Expose source and current-working-directory Skills through one backend."""

    source_skills_root = Path(project_root) / "skills"
    source_backend = FilesystemBackend(
        root_dir=source_skills_root,
        virtual_mode=True,
    )
    return CompositeBackend(
        default=working_backend or source_backend,
        routes={
            SOURCE_SKILLS_SOURCE: source_backend,
            CWD_SKILLS_SOURCE: (
                _WorkingDirectorySkillsBackend(source_skills_root)
                if working_backend is not None
                else source_backend
            ),
        },
    )

def create_project_skills_middleware(
    backend: BackendProtocol,
) -> SkillsMiddleware:
    """Create the upstream middleware that owns Skill discovery and prompting."""

    return _ProjectSkillsMiddleware(
        backend=backend,
        sources=[
            (SOURCE_SKILLS_SOURCE, "Source"),
            (CWD_SKILLS_SOURCE, "Working Directory"),
        ],
        system_prompt=_PROJECT_SKILLS_SYSTEM_PROMPT,
    )


def create_project_filesystem_middleware(
    backend: BackendProtocol,
    *,
    tools: tuple[str, ...] = PROJECT_FILESYSTEM_TOOL_NAMES,
) -> FilesystemMiddleware:
    """Expose governed upstream Deep Agents file tools for the repository."""

    middleware = FilesystemMiddleware(
        backend=backend,
        tools=list(tools),
    )
    for filesystem_tool in middleware.tools:
        filesystem_tool.metadata = {
            **(filesystem_tool.metadata or {}),
            "source": "deepagents",
        }
    return middleware


def native_skill_metadata(
    state: Mapping[str, Any],
) -> tuple[_ProjectSkillMetadata, ...]:
    """Return validated Agent Skills metadata from middleware-private state."""

    raw = state.get("skills_metadata")
    if not isinstance(raw, list):
        return ()
    result: list[_ProjectSkillMetadata] = []
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
        result.append({"name": name, "path": path, "description": description})
    return tuple(result)


def list_skill_reference_ids(
    backend: BackendProtocol,
    metadata: _ProjectSkillMetadata,
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
    "CWD_SKILLS_SOURCE",
    "PROJECT_FILESYSTEM_TOOL_NAMES",
    "PROJECT_FILESYSTEM_READ_TOOL_NAMES",
    "SOURCE_SKILLS_SOURCE",
    "create_project_skills_backend",
    "create_project_filesystem_middleware",
    "create_project_skills_middleware",
    "list_skill_reference_ids",
    "native_skill_metadata",
]
