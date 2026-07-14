from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPOSITORY_ROOT
    / ".codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py"
)
PYTHON = "/home/lenovo1/miniconda3/envs/hello_agent/bin/python"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def _write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "docs@example.test")
    _git(tmp_path, "config", "user.name", "Docs Test")
    _write(tmp_path, "README.md", "# Home\n\nSee [guide](docs/guide.md#setup).\n")
    _write(tmp_path, "AGENTS.md", "# Agent entry\n")
    _write(tmp_path, "docs/guide.md", "# Guide\n\n## Setup\n\nUse `src/app.py`.\n")
    _write(tmp_path, "docs/development/runbook.md", "# Runbook\n")
    _write(tmp_path, "docs/interview/questions.md", "# Questions\n")
    _write(tmp_path, "docs/superpowers/specs/design.md", "# Design\n")
    _write(tmp_path, ".codex/skills/example/SKILL.md", "# Skill\n")
    _write(tmp_path, "haodanku-openapi-docs/index.md", "# API docs\n")
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def _run(repo: Path, git_range: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [PYTHON, str(SCRIPT), "--repo-root", str(repo)]
    if git_range is not None:
        command.extend(["--git-range", git_range])
    return subprocess.run(command, text=True, capture_output=True)


def _payload(repo: Path, git_range: str | None = None) -> dict[str, object]:
    result = _run(repo, git_range)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_inventory_is_sorted_and_classifies_document_locations(repository: Path) -> None:
    payload = _payload(repository)

    assert payload["schema_version"] == 1
    assert payload["git_range"] is None
    assert payload["git_changes"] == {
        "requested": False,
        "added": [],
        "modified": [],
        "deleted": [],
        "renamed": [],
    }
    documents = payload["documents"]
    paths = [document["path"] for document in documents]
    assert paths == sorted(paths)
    locations = {document["path"]: document["location"] for document in documents}
    assert locations == {
        ".codex/skills/example/SKILL.md": "project_skill",
        "AGENTS.md": "repository_entry",
        "README.md": "repository_entry",
        "docs/development/runbook.md": "development",
        "docs/guide.md": "docs",
        "docs/interview/questions.md": "interview",
        "docs/superpowers/specs/design.md": "development_artifact",
        "haodanku-openapi-docs/index.md": "other",
    }


def test_repository_root_must_be_git_top_level(repository: Path) -> None:
    result = _run(repository / "docs")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Git top-level" in result.stderr


def test_reports_links_anchors_inbound_references_and_last_touch(repository: Path) -> None:
    payload = _payload(repository)
    checks = payload["checks"]["markdown_links"]
    guide_link = next(item for item in checks if item["target"] == "docs/guide.md#setup")
    assert guide_link["status"] == "ok"
    assert guide_link["resolved_path"] == "docs/guide.md"

    guide = next(item for item in payload["documents"] if item["path"] == "docs/guide.md")
    assert guide["last_touch_commit"] == _git(repository, "rev-parse", "HEAD")
    assert guide["inbound_references"] == [
        {"source": "README.md", "line": 3, "target": "docs/guide.md#setup", "kind": "markdown_link"}
    ]


def test_reports_external_missing_and_bad_anchor_links(repository: Path) -> None:
    _write(
        repository,
        "docs/links.md",
        "# Links\n\n[web](https://example.test/x) [local](missing.md) "
        "[bad anchor](guide.md#absent) [same](#links)\n",
    )
    checks = _payload(repository)["checks"]["markdown_links"]
    statuses = {item["target"]: item["status"] for item in checks if item["source"] == "docs/links.md"}
    assert statuses == {
        "https://example.test/x": "external",
        "missing.md": "missing",
        "guide.md#absent": "missing_anchor",
        "#links": "ok",
    }


def test_fenced_code_does_not_contribute_links_paths_or_headings(repository: Path) -> None:
    _write(
        repository,
        "docs/fenced.md",
        "# Real\n\n"
        "```markdown\n"
        "## Fake\n"
        "[hidden](missing.md)\n"
        "Use `tests/hidden_test.py`.\n"
        "```\n\n"
        "[real](#real) [fake](#fake)\n",
    )

    payload = _payload(repository)
    link_checks = [
        item for item in payload["checks"]["markdown_links"]
        if item["source"] == "docs/fenced.md"
    ]
    assert [(item["target"], item["status"]) for item in link_checks] == [
        ("#fake", "missing_anchor"),
        ("#real", "ok"),
    ]
    assert not any(
        item["source"] == "docs/fenced.md"
        for item in payload["checks"]["repository_paths"]
    )


def test_fence_marker_with_info_suffix_does_not_close_code_block(repository: Path) -> None:
    _write(
        repository,
        "docs/fence-close.md",
        "# Visible\n\n"
        "```markdown\n"
        "```not-a-close\n"
        "[hidden](missing.md)\n"
        "## Hidden heading\n"
        "```\n\n"
        "[visible](#visible) [hidden heading](#hidden-heading)\n",
    )

    checks = [
        item
        for item in _payload(repository)["checks"]["markdown_links"]
        if item["source"] == "docs/fence-close.md"
    ]

    assert [(item["target"], item["status"]) for item in checks] == [
        ("#hidden-heading", "missing_anchor"),
        ("#visible", "ok"),
    ]


def test_inventory_excludes_markdown_symlinks_outside_repository(
    repository: Path,
) -> None:
    outside = repository.parent / f"{repository.name}-outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (repository / "docs" / "outside.md").symlink_to(outside)

    paths = [item["path"] for item in _payload(repository)["documents"]]

    assert "docs/outside.md" not in paths


@pytest.mark.parametrize("entry", ["README.md", "AGENTS.md"])
def test_root_entry_symlink_outside_repository_is_not_read_or_inventoried(
    repository: Path, entry: str
) -> None:
    outside = repository.parent / f"{repository.name}-{entry}"
    outside.write_text(
        "# Outside\n\n[secret](missing-secret.md) Use `tests/secret.py`.\n",
        encoding="utf-8",
    )
    (repository / entry).unlink()
    (repository / entry).symlink_to(outside)

    payload = _payload(repository)

    assert entry not in [item["path"] for item in payload["documents"]]
    assert not any(
        item["source"] == entry
        for check_group in payload["checks"].values()
        for item in check_group
    )


def test_inline_path_symlink_outside_repository_is_structured(
    repository: Path,
) -> None:
    outside = repository.parent / f"{repository.name}-outside.py"
    outside.write_text("SECRET = True\n", encoding="utf-8")
    (repository / "docs" / "escape.py").symlink_to(outside)
    _write(repository, "docs/symlink.md", "Use `docs/escape.py`.\n")

    result = _run(repository)

    assert result.returncode == 0, result.stderr
    item = next(
        item for item in json.loads(result.stdout)["checks"]["repository_paths"]
        if item["source"] == "docs/symlink.md"
    )
    assert item == {
        "source": "docs/symlink.md",
        "line": 1,
        "target": "docs/escape.py",
        "resolved_path": None,
        "status": "outside_repository",
    }


def test_repository_path_checks_validate_concrete_paths_and_skip_globs(repository: Path) -> None:
    _write(
        repository,
        "docs/paths.md",
        "# Paths\n\nUse `src/app.py`, `docs/guide.md`, `tests/missing_test.py`, and `docs/**/*.md`.\n",
    )
    checks = _payload(repository)["checks"]["repository_paths"]
    statuses = {item["target"]: item["status"] for item in checks if item["source"] == "docs/paths.md"}
    assert statuses == {
        "src/app.py": "ok",
        "docs/guide.md": "ok",
        "tests/missing_test.py": "missing",
        "docs/**/*.md": "skipped_glob",
    }
    guide = next(item for item in _payload(repository)["documents"] if item["path"] == "docs/guide.md")
    assert {reference["kind"] for reference in guide["inbound_references"]} == {
        "markdown_link",
        "repository_path",
    }


def test_git_range_classifies_add_modify_delete_and_rename(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _write(repository, "README.md", "# Updated\n")
    _write(repository, "docs/new.md", "# New\n")
    _git(repository, "mv", "docs/guide.md", "docs/renamed-guide.md")
    (repository / "docs/development/runbook.md").unlink()
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "docs changes")

    changes = _payload(repository, f"{base}..HEAD")["git_changes"]
    assert changes == {
        "requested": True,
        "added": ["docs/new.md"],
        "modified": ["README.md"],
        "deleted": ["docs/development/runbook.md"],
        "renamed": [{"from": "docs/guide.md", "to": "docs/renamed-guide.md"}],
    }


@pytest.mark.parametrize("kind", ["not_git", "bad_range"])
def test_invalid_repository_or_range_returns_explainable_error(tmp_path: Path, kind: str) -> None:
    if kind == "not_git":
        result = _run(tmp_path)
        expected = "not a Git repository"
    else:
        _git(tmp_path, "init", "-q")
        result = _run(tmp_path, "missing..HEAD")
        expected = "invalid Git range"
    assert result.returncode != 0
    assert result.stdout == ""
    assert expected in result.stderr


def test_option_like_git_range_is_rejected_as_revision(repository: Path) -> None:
    result = subprocess.run(
        [
            PYTHON,
            str(SCRIPT),
            "--repo-root",
            str(repository),
            "--git-range=--all",
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "invalid Git range '--all'" in result.stderr


def test_running_collector_does_not_change_tracked_files(repository: Path) -> None:
    def tracked_hashes() -> dict[str, str]:
        return {
            path: hashlib.sha256((repository / path).read_bytes()).hexdigest()
            for path in _git(repository, "ls-files").splitlines()
        }

    before_hashes = tracked_hashes()
    before_status = _git(repository, "status", "--porcelain=v1")
    _payload(repository)
    assert tracked_hashes() == before_hashes
    assert _git(repository, "status", "--porcelain=v1") == before_status
