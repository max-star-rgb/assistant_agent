"""Validate repository-local Codex skills without network access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key\s*[:=]\s*[^\s]+|authorization\s*[:=]\s*[^\s]+|bearer\s+(?!token\b|tokens\b)[a-z0-9._~+/=-]{8,}|sk-[a-z0-9._-]{8,})"
)
REQUIRED_FIELDS = {"name", "description", "version"}


def main() -> int:
    result = validate_skills(SKILLS_DIR)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def validate_skills(skills_dir: Path) -> dict:
    errors: list[dict[str, str]] = []
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    if not skill_files:
        errors.append({"path": str(skills_dir), "error": "no_skill_files"})
    for path in skill_files:
        errors.extend(_validate_skill_file(path))
    return {
        "ok": not errors,
        "skill_count": len(skill_files),
        "skills": [path.parent.name for path in skill_files],
        "errors": errors,
    }


def _validate_skill_file(path: Path) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    text = path.read_text(encoding="utf-8")
    if SECRET_RE.search(text):
        errors.append({"path": str(path), "error": "possible_secret"})
    if not text.startswith("---\n"):
        return [*errors, {"path": str(path), "error": "missing_frontmatter"}]
    end = text.find("\n---\n", 4)
    if end == -1:
        return [*errors, {"path": str(path), "error": "unterminated_frontmatter"}]
    fields = _parse_frontmatter(text[4:end])
    missing = REQUIRED_FIELDS.difference(fields)
    for field in sorted(missing):
        errors.append({"path": str(path), "error": f"missing_{field}"})
    name = fields.get("name", "")
    if name and not NAME_RE.match(name):
        errors.append({"path": str(path), "error": "invalid_name"})
    if name and name != path.parent.name:
        errors.append({"path": str(path), "error": "name_directory_mismatch"})
    if "publish remote mcp" in text.lower() and "do not publish remote mcp" not in text.lower():
        errors.append({"path": str(path), "error": "unsafe_publish_instruction"})
    return errors


def _parse_frontmatter(frontmatter: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


if __name__ == "__main__":
    raise SystemExit(main())
