"""Load repo-local SKILL.md capability descriptors for assistant context."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class SkillDescriptor(BaseModel):
    """Prompt-safe repo-local skill descriptor backed by governed tools."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled: bool = True
    disable_model_invocation: bool = False
    governed_tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    required_inputs_by_tool: dict[str, list[str]] = Field(default_factory=dict)
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    safe_examples: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)


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


_ALLOWED_SECTION_TITLES = {
    "governed tools": "governed_tools",
    "permissions": "permissions",
    "required inputs": "required_inputs",
    "when to use": "when_to_use",
    "when not to use": "when_not_to_use",
    "safe examples": "safe_examples",
    "runtime constraints": "runtime_constraints",
}


def load_repo_skill_descriptors(root: Path) -> SkillCatalog:
    """Load prompt-safe descriptors from ``<root>/skills/<skill_id>/SKILL.md``."""

    root = Path(root)
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return SkillCatalog()

    descriptors: list[SkillDescriptor] = []
    issues: list[SkillLoadIssue] = []
    for skill_dir in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
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

    try:
        descriptor = SkillDescriptor(
            name=name,
            description=description,
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
