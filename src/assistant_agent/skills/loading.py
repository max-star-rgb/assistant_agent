"""Load repo-local Skill manifests and procedural guidance."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


SkillActivation = Literal["model", "context"]


class _SkillManifest(BaseModel):
    """Machine-readable contract stored in ``skill.toml``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    skill_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: int = Field(ge=1)
    description: str = Field(min_length=1)
    enabled: bool = True
    discoverable: bool = True
    disable_model_invocation: bool = False
    activation: SkillActivation = "model"
    governed_tools: list[str] = Field(min_length=1)
    references: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_tool_names(self) -> "_SkillManifest":
        normalized = [name.strip() for name in self.governed_tools]
        if any(not name for name in normalized):
            raise ValueError("governed_tools must not contain blank names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("governed_tools must not contain duplicates")
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None
            for name in normalized
        ):
            raise ValueError("governed_tools contains an invalid tool name")
        if self.activation == "context" and (
            self.discoverable or not self.disable_model_invocation
        ):
            raise ValueError(
                "context Skills must be undiscoverable and disable model invocation"
            )
        self.governed_tools = normalized
        return self


class SkillDescriptor(BaseModel):
    """Prompt-safe repo-local skill descriptor backed by governed tools."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1, exclude=True)
    manifest_version: int = Field(default=1, ge=1)
    enabled: bool = True
    discoverable: bool = True
    disable_model_invocation: bool = False
    activation: SkillActivation = "model"
    governed_tools: list[str] = Field(default_factory=list)
    references: dict[str, str] = Field(default_factory=dict)


class SkillLoadIssue(BaseModel):
    """Non-fatal issue found while loading one repo-local skill."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    skill_id: str | None = None
    path: str = ""


class SkillCatalog(BaseModel):
    """Loaded repo-local skills plus non-fatal load issues."""

    descriptors: list[SkillDescriptor] = Field(default_factory=list)
    issues: list[SkillLoadIssue] = Field(default_factory=list)


def render_skill_guidance(
    descriptor: SkillDescriptor,
) -> str:
    """Return the complete trusted Skill body after frontmatter."""

    return descriptor.body


def render_skill_activation_summary(
    descriptor: SkillDescriptor,
) -> str:
    """Return the small L0 description used inside the Skill index."""

    return descriptor.description


_RESERVED_SKILL_DIRECTORIES = {"manifests"}
_MACHINE_SECTION_TITLES = {
    "governed tools",
    "受治理工具",
    "permissions",
    "权限",
    "required inputs",
    "必需输入",
    "references",
    "参考资料",
    "visibility",
    "可见性",
    "tests",
    "测试",
}


def default_repo_root() -> Path:
    """Return the repository root that owns this package's project Skills."""

    return Path(__file__).resolve().parents[3]


def read_registered_skill_reference(
    root: Path,
    descriptor: SkillDescriptor,
    reference_id: str,
    *,
    max_chars: int = 20_000,
) -> str | None:
    """Read one descriptor-declared reference without accepting a file path."""

    reference_path = descriptor.references.get(reference_id)
    if reference_path is None:
        return None
    skills_dir = Path(root).resolve() / "skills"
    skill_dir = skills_dir / descriptor.name
    references_dir = skill_dir / "references"
    candidate = skill_dir / reference_path
    if (
        skills_dir.is_symlink()
        or skill_dir.is_symlink()
        or references_dir.is_symlink()
        or candidate.is_symlink()
    ):
        return None
    try:
        resolved_skill_dir = skill_dir.resolve(strict=True)
        resolved_references_dir = references_dir.resolve(strict=True)
        path = candidate.resolve(strict=True)
        resolved_references_dir.relative_to(resolved_skill_dir)
        path.relative_to(resolved_references_dir)
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
        return None
    if len(content) > max_chars:
        return None
    return content


def load_repo_skill_descriptors(root: Path) -> SkillCatalog:
    """Load prompt-safe descriptors from project ``skill.toml`` files."""

    root = Path(root)
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return SkillCatalog()
    if skills_dir.is_symlink():
        return SkillCatalog(
            issues=[
                _issue(
                    "skills_directory_symlink_not_allowed",
                    "The project skills directory must not be a symbolic link.",
                    root=root,
                    path=skills_dir,
                    skill_id=None,
                )
            ]
        )

    descriptors: list[SkillDescriptor] = []
    issues: list[SkillLoadIssue] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name in _RESERVED_SKILL_DIRECTORIES:
            continue
        if skill_dir.is_symlink():
            issues.append(
                _issue(
                    "skill_symlink_not_allowed",
                    "Skill directories must not be symbolic links.",
                    root=root,
                    path=skill_dir,
                    skill_id=skill_dir.name,
                )
            )
            continue
        skill_file = skill_dir / "SKILL.md"
        manifest_file = skill_dir / "skill.toml"
        if not manifest_file.is_file():
            issues.append(
                _issue(
                    "missing_skill_manifest",
                    "Skill directory does not contain skill.toml.",
                    root=root,
                    path=manifest_file,
                    skill_id=skill_dir.name,
                )
            )
            continue
        if manifest_file.is_symlink():
            issues.append(
                _issue(
                    "skill_manifest_symlink_not_allowed",
                    "skill.toml must not be a symbolic link.",
                    root=root,
                    path=manifest_file,
                    skill_id=skill_dir.name,
                )
            )
            continue
        if not skill_file.is_file():
            issues.append(
                _issue(
                    "missing_skill_file",
                    "Skill directory does not contain SKILL.md.",
                    root=root,
                    path=skill_file,
                    skill_id=skill_dir.name,
                )
            )
            continue
        if skill_file.is_symlink():
            issues.append(
                _issue(
                    "skill_file_symlink_not_allowed",
                    "SKILL.md must not be a symbolic link.",
                    root=root,
                    path=skill_file,
                    skill_id=skill_dir.name,
                )
            )
            continue
        descriptor, skill_issues = _load_skill_files(
            root,
            skill_dir.name,
            manifest_file,
            skill_file,
        )
        issues.extend(skill_issues)
        if descriptor is not None:
            descriptors.append(descriptor)
    return SkillCatalog(descriptors=descriptors, issues=issues)


def _load_skill_files(
    root: Path,
    skill_id: str,
    manifest_file: Path,
    skill_file: Path,
) -> tuple[SkillDescriptor | None, list[SkillLoadIssue]]:
    try:
        manifest_payload = tomllib.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, [
            _issue(
                "skill_manifest_read_failed",
                f"Could not read skill.toml: {exc}",
                root=root,
                path=manifest_file,
                skill_id=skill_id,
            )
        ]
    try:
        manifest = _SkillManifest.model_validate(manifest_payload)
    except ValidationError as exc:
        return None, [
            _issue(
                "invalid_skill_manifest",
                f"skill.toml failed validation: {exc}",
                root=root,
                path=manifest_file,
                skill_id=skill_id,
            )
        ]
    if manifest.skill_id != skill_id:
        return None, [
            _issue(
                "skill_id_mismatch",
                "skill.toml skill_id must match the containing directory name.",
                root=root,
                path=manifest_file,
                skill_id=skill_id,
            )
        ]
    if not manifest.enabled:
        return None, [
            _issue(
                "skill_disabled",
                "skill.toml has enabled = false.",
                root=root,
                path=manifest_file,
                skill_id=skill_id,
            )
        ]
    try:
        content = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [
            _issue(
                "skill_markdown_read_failed",
                f"Could not read SKILL.md: {exc}",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    normalized_body = content.strip()
    if not normalized_body:
        return None, [
            _issue(
                "empty_skill_markdown",
                "SKILL.md must contain procedural guidance.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    if normalized_body.startswith("---") or _contains_machine_contract_section(
        normalized_body
    ):
        return None, [
            _issue(
                "skill_markdown_contains_machine_contract",
                "SKILL.md must contain procedural guidance only; use skill.toml for machine fields.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    reference_issue = _validate_references(
        root=root,
        skill_id=skill_id,
        references=manifest.references,
    )
    if reference_issue is not None:
        return None, [reference_issue]

    try:
        descriptor = SkillDescriptor(
            name=manifest.skill_id,
            description=manifest.description,
            body=normalized_body,
            manifest_version=manifest.version,
            enabled=manifest.enabled,
            discoverable=manifest.discoverable,
            disable_model_invocation=manifest.disable_model_invocation,
            activation=manifest.activation,
            governed_tools=manifest.governed_tools,
            references=manifest.references,
        )
    except ValidationError as exc:
        return None, [
            _issue(
                "skill_descriptor_validation_failed",
                f"Skill descriptor failed validation: {exc}",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    return descriptor, []


def _contains_machine_contract_section(body: str) -> bool:
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("## "):
            continue
        title = re.sub(r"\s+", " ", stripped[3:].strip().lower())
        if title in _MACHINE_SECTION_TITLES:
            return True
    return False


def _validate_references(
    *,
    root: Path,
    skill_id: str,
    references: dict[str, str],
) -> SkillLoadIssue | None:
    skill_dir = root / "skills" / skill_id
    for reference_id, reference_path in references.items():
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", reference_id) is None:
            return _issue(
                "invalid_reference_id",
                "Skill reference ids must use lowercase letters, digits, and hyphens.",
                root=root,
                path=skill_dir / reference_path,
                skill_id=skill_id,
            )
        if (
            re.fullmatch(r"references/[A-Za-z0-9][A-Za-z0-9_.-]*\.md", reference_path)
            is None
        ):
            return _issue(
                "invalid_reference_path",
                "Skill references must be one-level Markdown files under references/.",
                root=root,
                path=skill_dir / reference_path,
                skill_id=skill_id,
            )
        references_dir = skill_dir / "references"
        candidate = skill_dir / reference_path
        if references_dir.is_symlink() or candidate.is_symlink():
            return _issue(
                "reference_symlink_not_allowed",
                "Skill reference directories and files must not be symbolic links.",
                root=root,
                path=candidate,
                skill_id=skill_id,
            )
        try:
            resolved_skill_dir = skill_dir.resolve(strict=True)
            resolved_references_dir = references_dir.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
            resolved_references_dir.relative_to(resolved_skill_dir)
            resolved.relative_to(resolved_references_dir)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return _issue(
                "invalid_reference_path",
                "Skill reference is missing or outside the registered Skill directory.",
                root=root,
                path=skill_dir / reference_path,
                skill_id=skill_id,
            )
        if not resolved.is_file():
            return _issue(
                "invalid_reference_path",
                "Skill reference must be a regular Markdown file.",
                root=root,
                path=resolved,
                skill_id=skill_id,
            )
    return None


def _issue(
    code: str,
    message: str,
    *,
    root: Path,
    path: Path,
    skill_id: str | None,
) -> SkillLoadIssue:
    return SkillLoadIssue(
        code=code,
        message=message,
        skill_id=skill_id,
        path=_relative_path(root, path),
    )


def _relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
