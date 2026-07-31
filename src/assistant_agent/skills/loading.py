"""Load repo-local SKILL.md capability descriptors for assistant context."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class SkillVisibility(BaseModel):
    """Prompt-safe visibility declaration for one repo-local skill."""

    tags: list[str] = Field(default_factory=list)
    enabled_by_default: bool = True
    skill_only: bool = False


class SkillDescriptor(BaseModel):
    """Prompt-safe repo-local skill descriptor backed by governed tools."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    manifest_version: int = Field(default=1, ge=1)
    enabled: bool = True
    disable_model_invocation: bool = False
    governed_tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    required_inputs_by_tool: dict[str, list[str]] = Field(default_factory=dict)
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    safe_examples: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)
    activation_summary: list[str] = Field(default_factory=list)
    references: dict[str, str] = Field(default_factory=dict)
    visibility: SkillVisibility = Field(default_factory=SkillVisibility)
    tests: list[str] = Field(default_factory=list)


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
    *,
    available_tool_names: set[str],
) -> str:
    """Render one prompt-safe procedural projection for the active tool catalog."""

    governed_tools = [
        tool_name
        for tool_name in descriptor.governed_tools
        if tool_name in available_tool_names
    ]
    sections = [
        f"# 项目 Skill：{descriptor.name}",
        descriptor.description,
        _render_guidance_list("适用条件", descriptor.when_to_use),
        _render_guidance_list("不适用条件", descriptor.when_not_to_use),
        _render_required_inputs(
            descriptor.required_inputs_by_tool,
            available_tool_names=set(governed_tools),
        ),
        _render_guidance_list("安全示例", descriptor.safe_examples),
        _render_guidance_list("运行约束", descriptor.runtime_constraints),
        _render_reference_ids(descriptor.references),
    ]
    return "\n\n".join(section for section in sections if section)


def render_skill_activation_summary(
    descriptor: SkillDescriptor,
    *,
    available_tool_names: set[str],
) -> str:
    """Render the small L0 projection that lets the model discover one Skill."""

    summary_items = descriptor.activation_summary or descriptor.when_to_use[:3]
    sections = [
        f"# 可用项目 Skill：{descriptor.name}",
        _render_guidance_list("快速路由", summary_items),
    ]
    if "load_skill" in available_tool_names:
        sections.append(
            "需要执行该领域任务时，先调用 "
            f'load_skill({{"skill_id":"{descriptor.name}"}}) '
            "读取完整工作流；需要专项细节时再按返回的 reference_ids 调用 "
            "load_skill_reference。"
        )
    return "\n\n".join(section for section in sections if section)


_ALLOWED_SECTION_TITLES = {
    "governed tools": "governed_tools",
    "permissions": "permissions",
    "required inputs": "required_inputs",
    "when to use": "when_to_use",
    "when not to use": "when_not_to_use",
    "safe examples": "safe_examples",
    "runtime constraints": "runtime_constraints",
    "activation summary": "activation_summary",
    "references": "references",
    "visibility": "visibility",
    "tests": "tests",
}
_RESERVED_SKILL_DIRECTORIES = {"manifests"}


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
    """Load prompt-safe descriptors from ``<root>/skills/<skill_id>/SKILL.md``."""

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
        descriptor, skill_issues = _load_skill_file(root, skill_dir.name, skill_file)
        issues.extend(skill_issues)
        if descriptor is not None:
            descriptors.append(descriptor)
    return SkillCatalog(descriptors=descriptors, issues=issues)


def _load_skill_file(
    root: Path,
    skill_id: str,
    skill_file: Path,
) -> tuple[SkillDescriptor | None, list[SkillLoadIssue]]:
    issues: list[SkillLoadIssue] = []
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [
            _issue(
                "read_failed",
                f"Could not read SKILL.md: {exc}",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    frontmatter, body = _split_frontmatter(content)
    if frontmatter is None:
        return None, [
            _issue(
                "missing_frontmatter",
                "SKILL.md must start with frontmatter delimited by ---.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    metadata = _parse_frontmatter(frontmatter)
    name = _metadata_text(metadata, "name")
    if not name:
        return None, [
            _issue(
                "missing_name",
                "Frontmatter field 'name' is required.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    if name != skill_id:
        return None, [
            _issue(
                "name_mismatch",
                "Frontmatter field 'name' must match the containing directory name.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    description = _metadata_text(metadata, "description")
    if not description:
        return None, [
            _issue(
                "missing_description",
                "Frontmatter field 'description' is required.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    enabled = _metadata_bool(metadata, "enabled", default=True)
    if not enabled:
        return None, [
            _issue(
                "skill_disabled",
                "Skill frontmatter has enabled: false.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    disable_model_invocation = _metadata_bool(
        metadata,
        "disable-model-invocation",
        default=False,
    )
    if disable_model_invocation:
        return None, [
            _issue(
                "model_invocation_disabled",
                "Skill frontmatter disables model invocation.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    sections = _parse_sections(body)
    governed_tools = _tool_names_from_section(sections.get("governed_tools", []))
    if not governed_tools:
        return None, [
            _issue(
                "missing_governed_tools",
                "SKILL.md must include at least one governed tool.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    permissions = _permissions_from_section(sections.get("permissions", []))
    invalid_permissions = [
        permission for permission in permissions if not _is_valid_tool_permission(permission)
    ]
    if invalid_permissions:
        return None, [
            _issue(
                "invalid_permission",
                "Skill permissions must use the v1 tool:<name> vocabulary.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]
    missing_tool_permissions = [
        tool_name for tool_name in governed_tools if f"tool:{tool_name}" not in permissions
    ]
    if missing_tool_permissions:
        return None, [
            _issue(
                "missing_tool_permission",
                "Every governed tool must have a matching tool:<name> permission.",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        ]

    references = _references_from_section(sections.get("references", []))
    reference_issue = _validate_references(
        root=root,
        skill_id=skill_id,
        references=references,
    )
    if reference_issue is not None:
        return None, [reference_issue]

    try:
        descriptor = SkillDescriptor(
            name=name,
            description=description,
            manifest_version=_metadata_int(metadata, "manifest-version", default=1),
            enabled=enabled,
            disable_model_invocation=disable_model_invocation,
            governed_tools=governed_tools,
            permissions=permissions,
            required_inputs_by_tool=_required_inputs_from_section(
                sections.get("required_inputs", [])
            ),
            when_to_use=_list_items_from_section(sections.get("when_to_use", [])),
            when_not_to_use=_list_items_from_section(sections.get("when_not_to_use", [])),
            safe_examples=_list_items_from_section(sections.get("safe_examples", [])),
            runtime_constraints=_list_items_from_section(
                sections.get("runtime_constraints", [])
            ),
            activation_summary=_list_items_from_section(
                sections.get("activation_summary", [])
            ),
            references=references,
            visibility=_visibility_from_section(sections.get("visibility", [])),
            tests=_list_items_from_section(sections.get("tests", [])),
        )
    except ValidationError as exc:
        issues.append(
            _issue(
                "validation_failed",
                f"SKILL.md descriptor failed validation: {exc}",
                root=root,
                path=skill_file,
                skill_id=skill_id,
            )
        )
        return None, issues
    return descriptor, issues


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, content
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    return None, content


def _parse_frontmatter(frontmatter: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for raw_line in frontmatter.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        metadata[key] = _parse_scalar(raw_value.strip())
    return metadata


def _parse_scalar(value: str) -> Any:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1]
    return value


def _parse_sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = _normalize_title(stripped[3:])
            current = _ALLOWED_SECTION_TITLES.get(title)
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _tool_names_from_section(lines: list[str]) -> list[str]:
    names: list[str] = []
    for item in _list_items_from_section(lines):
        for candidate in item.split(","):
            name = _clean_token(candidate)
            if name:
                names.append(name)
    return _unique(names)


def _required_inputs_from_section(lines: list[str]) -> dict[str, list[str]]:
    required_inputs: dict[str, list[str]] = {}
    for item in _list_items_from_section(lines):
        if ":" not in item:
            continue
        raw_tool, raw_inputs = item.split(":", 1)
        tool_name = _clean_token(raw_tool)
        inputs = [
            _clean_token(value)
            for value in re.split(r"[, ]+", raw_inputs.strip().strip("[]"))
        ]
        inputs = [value for value in inputs if value]
        if tool_name and inputs:
            required_inputs[tool_name] = _unique(inputs)
    return required_inputs


def _permissions_from_section(lines: list[str]) -> list[str]:
    permissions: list[str] = []
    for item in _list_items_from_section(lines):
        for candidate in item.split(","):
            permission = _clean_token(candidate)
            if permission:
                permissions.append(permission)
    return _unique(permissions)


def _references_from_section(lines: list[str]) -> dict[str, str]:
    references: dict[str, str] = {}
    for item in _list_items_from_section(lines):
        if ":" not in item:
            continue
        raw_id, raw_path = item.split(":", 1)
        reference_id = _clean_token(raw_id)
        reference_path = _clean_token(raw_path)
        if reference_id and reference_path:
            references[reference_id] = reference_path
    return references


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


def _visibility_from_section(lines: list[str]) -> SkillVisibility:
    values: dict[str, Any] = {}
    for item in _list_items_from_section(lines):
        if ":" not in item:
            continue
        raw_key, raw_value = item.split(":", 1)
        key = raw_key.strip().replace("-", "_").lower()
        value = raw_value.strip()
        if key == "tags":
            values["tags"] = [
                _clean_token(candidate)
                for candidate in value.split(",")
                if _clean_token(candidate)
            ]
        elif key == "enabled_by_default":
            values["enabled_by_default"] = _bool_from_text(value, default=True)
        elif key == "skill_only":
            values["skill_only"] = _bool_from_text(value, default=False)
    return SkillVisibility(**values)


def _is_valid_tool_permission(permission: str) -> bool:
    return re.match(r"^tool:[A-Za-z0-9_.-]+$", permission) is not None


def _list_items_from_section(lines: list[str]) -> list[str]:
    items: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        item = _strip_bullet(line)
        if item:
            items.append(item)
    return items


def _strip_bullet(line: str) -> str:
    if line.startswith(("- ", "* ")):
        return line[2:].strip()
    numbered = re.match(r"^\d+[\.)]\s+(?P<item>.+)$", line)
    if numbered:
        return numbered.group("item").strip()
    return ""


def _clean_token(value: str) -> str:
    return value.strip().strip("`'\"[]")


def _metadata_text(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


def _metadata_bool(metadata: dict[str, Any], key: str, *, default: bool) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default


def _metadata_int(metadata: dict[str, Any], key: str, *, default: int) -> int:
    value = metadata.get(key)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool_from_text(value: str, *, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    return default


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


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _render_guidance_list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    return "\n".join([f"## {title}", *(f"- {item}" for item in items)])


def _render_required_inputs(
    required_inputs_by_tool: dict[str, list[str]],
    *,
    available_tool_names: set[str],
) -> str:
    items = [
        f"- {tool_name}: {', '.join(inputs)}"
        for tool_name, inputs in required_inputs_by_tool.items()
        if tool_name in available_tool_names
    ]
    if not items:
        return ""
    return "\n".join(["## 必填输入", *items])


def _render_reference_ids(references: dict[str, str]) -> str:
    if not references:
        return ""
    return "\n".join(
        ["## 按需参考", *(f"- {reference_id}" for reference_id in references)]
    )
