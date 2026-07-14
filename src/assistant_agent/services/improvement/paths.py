"""Repository-contained target path resolution for Improvement Lab reads."""

from pathlib import Path


class ImprovementTargetPathError(ValueError):
    """Raised when a target path is missing, linked, or outside its root."""


def resolve_repo_skill_file(repo_root: Path, skill_id: str) -> Path:
    """Resolve one non-linked repo skill file under the configured skill root."""

    if skill_id in {".", ".."} or "/" in skill_id or "\\" in skill_id:
        raise ImprovementTargetPathError("skill target must be a safe single path segment")
    repo_resolved = Path(repo_root).resolve()
    skills_entry = Path(repo_root) / "skills"
    if skills_entry.is_symlink():
        raise ImprovementTargetPathError("skill root symlinks are not allowed")
    try:
        skills_root = skills_entry.resolve(strict=True)
        skills_root.relative_to(repo_resolved)
    except (OSError, ValueError) as exc:
        raise ImprovementTargetPathError("skill root is unavailable or outside the repository") from exc
    skill_dir = skills_root / skill_id
    skill_file = skill_dir / "SKILL.md"
    if skill_dir.is_symlink() or skill_file.is_symlink():
        raise ImprovementTargetPathError("skill target symlinks are not allowed")
    try:
        resolved = skill_file.resolve(strict=True)
        resolved.relative_to(skills_root)
    except (OSError, ValueError) as exc:
        raise ImprovementTargetPathError("skill target is unavailable or outside the skill root") from exc
    if not resolved.is_file():
        raise ImprovementTargetPathError("skill target must be a regular file")
    return resolved
