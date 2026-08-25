"""Agent Skills integration backed by Deep Agents' native middleware."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, LsResult
from deepagents.middleware import SkillsMiddleware
from deepagents.middleware.skills import SkillMetadata


PROJECT_SKILLS_SOURCE = "/"
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def create_project_skills_backend(skills_root: str | Path) -> FilesystemBackend:
    """Create the read source used by native Skill discovery and narrow loaders."""

    return FilesystemBackend(root_dir=Path(skills_root), virtual_mode=True)


def create_project_skills_middleware(
    backend: BackendProtocol,
) -> SkillsMiddleware:
    """Load Agent Skills metadata without exposing a generic file Tool."""

    return SkillsMiddleware(
        backend=backend,
        sources=[PROJECT_SKILLS_SOURCE],
        system_prompt=None,
    )


def load_project_skills_metadata(
    backend: BackendProtocol,
) -> tuple[SkillMetadata, ...]:
    """Load one trusted metadata snapshot through upstream SkillsMiddleware."""

    middleware = create_project_skills_middleware(backend)
    update = middleware.before_agent({}, None, {})  # type: ignore[arg-type]
    if update is None:
        return ()
    return native_skill_metadata(update)


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
        allowed_tools = item.get("allowed_tools")
        if not all(isinstance(value, str) and value for value in (name, path, description)):
            continue
        if _SKILL_ID_PATTERN.fullmatch(name) is None:
            continue
        skill_path = PurePosixPath(path)
        if skill_path.name != "SKILL.md" or skill_path.parent.name != name:
            continue
        if not isinstance(allowed_tools, list) or not all(
            isinstance(tool_name, str) and tool_name
            for tool_name in allowed_tools
        ):
            continue
        result.append(item)  # type: ignore[arg-type]
    return tuple(result)


def skill_metadata_by_name(
    state: Mapping[str, Any],
    skill_id: str,
) -> SkillMetadata | None:
    """Resolve one validated native Skill by its spec name."""

    return next(
        (
            metadata
            for metadata in native_skill_metadata(state)
            if metadata["name"] == skill_id
        ),
        None,
    )


def read_skill_content(
    backend: BackendProtocol,
    metadata: SkillMetadata,
) -> str | None:
    """Read one exact SKILL.md selected from trusted native metadata."""

    return _download_utf8(backend, metadata["path"])


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


def read_skill_reference(
    backend: BackendProtocol,
    metadata: SkillMetadata,
    reference_id: str,
) -> str | None:
    """Read one flat Markdown reference previously granted by Skill loading."""

    if _SKILL_ID_PATTERN.fullmatch(reference_id) is None:
        return None
    references_dir = PurePosixPath(metadata["path"]).parent / "references"
    return _download_utf8(backend, str(references_dir / f"{reference_id}.md"))


def _download_utf8(backend: BackendProtocol, path: str) -> str | None:
    responses = backend.download_files([path])
    if len(responses) != 1:
        return None
    response = responses[0]
    if response.error or response.content is None:
        return None
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return None


__all__ = [
    "PROJECT_SKILLS_SOURCE",
    "create_project_skills_backend",
    "create_project_skills_middleware",
    "list_skill_reference_ids",
    "load_project_skills_metadata",
    "native_skill_metadata",
    "read_skill_content",
    "read_skill_reference",
    "skill_metadata_by_name",
]
