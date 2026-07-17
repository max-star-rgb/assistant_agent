from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_SKILL_ROOT = REPO_ROOT / "skills"


def test_project_runtime_skills_have_valid_front_matter() -> None:
    skill_files = sorted(PROJECT_SKILL_ROOT.glob("*/SKILL.md"))

    assert skill_files, "expected at least one project runtime skill under skills/"
    for path in skill_files:
        metadata, body = _read_skill(path)
        skill_name = path.parent.name
        assert metadata.get("name") == skill_name
        assert metadata.get("description")
        assert body.strip()


def test_project_runtime_skill_names_are_unique() -> None:
    names = [
        _read_skill(path)[0]["name"]
        for path in sorted(PROJECT_SKILL_ROOT.glob("*/SKILL.md"))
    ]

    assert len(names) == len(set(names))


def test_project_runtime_skills_do_not_reference_missing_local_resources() -> None:
    for path in sorted(PROJECT_SKILL_ROOT.glob("*/SKILL.md")):
        for ref in _skill_resource_refs(path):
            assert ref.exists(), f"{path} references missing resource {ref}"


def _read_skill(path: Path) -> tuple[dict[str, str], str]:
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"{path} must start with YAML front matter"
    _, front_matter, body = raw.split("---", maxsplit=2)
    metadata: dict[str, str] = {}
    for line in front_matter.splitlines():
        if not line.strip():
            continue
        key, value = line.split(":", maxsplit=1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def _skill_resource_refs(path: Path) -> list[Path]:
    text = path.read_text(encoding="utf-8")
    skill_dir = path.parent
    skill_root_ref = f"skills/{skill_dir.name}/"
    refs: list[Path] = []
    for token in text.replace("`", " ").split():
        cleaned = token.strip(".,;:()[]{}\"'")
        if cleaned.startswith(("./scripts/", "./references/", "./templates/", "./assets/", "./fixtures/")):
            refs.append(skill_dir / cleaned.removeprefix("./"))
        elif cleaned.startswith(skill_root_ref):
            refs.append(REPO_ROOT / cleaned)
    return refs
