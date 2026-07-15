from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = (
    Path(__file__).parents[3]
    / ".codex/skills/assistant-agent-test-governance/scripts/collect_test_evidence.py"
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Test User")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _run_collector(
    root: Path,
    *,
    git_range: str | None = None,
    profile: str = "none",
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    command = [
        sys.executable,
        os.fspath(SCRIPT),
        "--repo-root",
        os.fspath(root),
        "--profile",
        profile,
    ]
    if git_range:
        command.extend(["--git-range", git_range])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    return completed, payload


@pytest.mark.fast
def test_collects_parameterized_nodes_effective_markers_imports_and_duplicates(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pytest.ini",
        "[pytest]\nmarkers =\n    fast: fast tests\n    contract: contract tests\n    dynamic: added during collection\n",
    )
    _write(
        tmp_path,
        "tests/conftest.py",
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        if item.name.startswith('test_double'):\n"
        "            item.add_marker('dynamic')\n",
    )
    _write(tmp_path, "app.py", "def double(value):\n    return value * 2\n")
    _write(
        tmp_path,
        "tests/test_sample.py",
        """\
import pytest
from app import double

pytestmark = pytest.mark.fast

@pytest.mark.parametrize("value", [1, 2])
def test_double(value):
    assert double(value) == value * 2

@pytest.mark.contract
def test_duplicate_one():
    assert double(2) == 4

@pytest.mark.contract
def test_duplicate_two():
    assert double(2) == 4

class TestDuplicateClassOne:
    def test_class_duplicate(self):
        assert double(3) == 6

class TestDuplicateClassTwo:
    def test_class_duplicate(self):
        assert double(3) == 6

class TestAsyncDuplicateOne:
    async def test_async_duplicate(self):
        assert double(4) == 8

class TestAsyncDuplicateTwo:
    async def test_async_duplicate(self):
        assert double(4) == 8
""",
    )

    completed, payload = _run_collector(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    expected_nodeids = [
        "tests/test_sample.py::TestAsyncDuplicateOne::test_async_duplicate",
        "tests/test_sample.py::TestAsyncDuplicateTwo::test_async_duplicate",
        "tests/test_sample.py::TestDuplicateClassOne::test_class_duplicate",
        "tests/test_sample.py::TestDuplicateClassTwo::test_class_duplicate",
        "tests/test_sample.py::test_double[1]",
        "tests/test_sample.py::test_double[2]",
        "tests/test_sample.py::test_duplicate_one",
        "tests/test_sample.py::test_duplicate_two",
    ]
    assert [item["nodeid"] for item in payload["tests"]] == sorted(expected_nodeids)
    duplicate_groups = {tuple(group["nodeids"]) for group in payload["duplicates"]}
    assert duplicate_groups == {
        (
            "tests/test_sample.py::TestAsyncDuplicateOne::test_async_duplicate",
            "tests/test_sample.py::TestAsyncDuplicateTwo::test_async_duplicate",
        ),
        (
            "tests/test_sample.py::TestDuplicateClassOne::test_class_duplicate",
            "tests/test_sample.py::TestDuplicateClassTwo::test_class_duplicate",
        ),
        (
            "tests/test_sample.py::test_duplicate_one",
            "tests/test_sample.py::test_duplicate_two",
        ),
    }
    assert all(group["nodeids"] for group in payload["duplicates"])
    by_node = {item["nodeid"]: item for item in payload["tests"]}
    assert by_node["tests/test_sample.py::test_double[1]"]["markers"] == [
        "dynamic",
        "fast",
        "parametrize",
    ]
    assert by_node["tests/test_sample.py::test_duplicate_one"]["markers"] == ["contract", "fast"]
    test_file = payload["test_files"][0]
    assert test_file["imports"] == ["app.double", "pytest"]
    assert "app.double" in test_file["targets"]


@pytest.mark.fast
def test_collects_git_changes_last_touch_and_inbound_references(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "tests/test_added_then_renamed.py", "def test_old():\n    assert True\n")
    _write(tmp_path, "tests/test_modified.py", "def test_value():\n    assert 1\n")
    _write(tmp_path, "tests/test_deleted.py", "def test_deleted():\n    assert True\n")
    base = _commit_all(tmp_path, "base")
    _git(tmp_path, "mv", "tests/test_added_then_renamed.py", "tests/test_renamed.py")
    _write(tmp_path, "tests/test_modified.py", "def test_value():\n    assert 2\n")
    (tmp_path / "tests/test_deleted.py").unlink()
    _write(tmp_path, "tests/test_added.py", "def test_added():\n    assert True\n")
    _write(tmp_path, "docs/testing.md", "Run `tests/test_modified.py` before release.\n")
    _write(tmp_path, "scripts/check.sh", "pytest tests/test_modified.py\n")
    head = _commit_all(tmp_path, "changes")

    completed, payload = _run_collector(tmp_path, git_range=f"{base}..{head}")

    assert completed.returncode == 0
    assert payload["git"]["range"] == f"{base}..{head}"
    assert [(item["status"], item["path"]) for item in payload["git"]["changes"]] == [
        ("A", "docs/testing.md"),
        ("A", "scripts/check.sh"),
        ("A", "tests/test_added.py"),
        ("D", "tests/test_deleted.py"),
        ("M", "tests/test_modified.py"),
        ("R", "tests/test_renamed.py"),
    ]
    renamed = next(item for item in payload["git"]["changes"] if item["status"] == "R")
    assert renamed["old_path"] == "tests/test_added_then_renamed.py"
    modified = next(item for item in payload["test_files"] if item["path"] == "tests/test_modified.py")
    assert modified["last_touch"]["commit"] == head
    assert [(ref["source"], ref["line"]) for ref in modified["inbound_references"]] == [
        ("docs/testing.md", 1),
        ("scripts/check.sh", 1),
    ]


@pytest.mark.fast
def test_profile_fast_reports_pass_fail_skip_xfail_and_durations(tmp_path: Path) -> None:
    _write(tmp_path, "pytest.ini", "[pytest]\nmarkers =\n    fast: fast tests\n")
    _write(
        tmp_path,
        "tests/test_statuses.py",
        """\
import pytest

@pytest.mark.fast
def test_pass():
    assert True

@pytest.mark.fast
def test_fail():
    assert False

@pytest.mark.fast
@pytest.mark.skip(reason="demonstrate skip")
def test_skip():
    pass

@pytest.mark.fast
@pytest.mark.xfail(reason="known issue")
def test_xfail():
    assert False

def test_not_fast():
    assert True
""",
    )

    completed, payload = _run_collector(tmp_path, profile="fast")

    assert completed.returncode == 0
    run = payload["profile_run"]
    assert run["pytest_exit_code"] == 1
    assert run["counts"] == {"failed": 1, "passed": 1, "skipped": 1, "xfailed": 1}
    assert [item["nodeid"] for item in run["results"]] == sorted(
        item["nodeid"] for item in run["results"]
    )
    assert all(item["duration_seconds"] >= 0 for item in run["results"])
    assert "tests/test_statuses.py::test_not_fast" not in {
        item["nodeid"] for item in run["results"]
    }


@pytest.mark.fast
def test_full_offline_excludes_integration_tests(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pytest.ini",
        "[pytest]\nmarkers =\n    integration: external integration tests\n",
    )
    _write(
        tmp_path,
        "tests/test_profiles.py",
        """\
import pytest

def test_offline():
    assert True

@pytest.mark.integration
def test_external():
    raise RuntimeError("must not run")
""",
    )

    completed, payload = _run_collector(tmp_path, profile="full-offline")

    assert completed.returncode == 0
    assert payload["profile_run"]["pytest_exit_code"] == 0
    assert payload["profile_run"]["counts"] == {"passed": 1}
    assert [item["nodeid"] for item in payload["profile_run"]["results"]] == [
        "tests/test_profiles.py::test_offline"
    ]


@pytest.mark.fast
def test_non_git_repo_and_invalid_range_return_structured_json(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_plain.py", "def test_plain():\n    assert True\n")
    completed, payload = _run_collector(tmp_path)
    assert completed.returncode == 0
    assert payload["git"] == {"available": False, "changes": [], "range": None}

    _init_repo(tmp_path)
    _commit_all(tmp_path, "initial")
    invalid, error_payload = _run_collector(tmp_path, git_range="missing..HEAD")
    assert invalid.returncode == 2
    assert error_payload["errors"][0]["code"] == "invalid_git_range"
    assert "missing..HEAD" in error_payload["errors"][0]["message"]


@pytest.mark.fast
def test_collector_does_not_change_tracked_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "tests/test_clean.py", "def test_clean():\n    assert True\n")
    _commit_all(tmp_path, "initial")
    before = _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all").stdout

    completed, _ = _run_collector(tmp_path, profile="fast")

    after = _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all").stdout
    assert completed.returncode == 0
    assert before == after == ""
