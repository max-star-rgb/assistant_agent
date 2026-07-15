import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/run_scoped_tests.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_scoped_tests_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _scope_map():
    return MODULE.ScopeMap(
        critical_paths=("tests/unit",),
        scopes=(
            MODULE.ScopeDefinition(
                name="gateway",
                source_paths=("src/assistant_agent/gateway/**",),
                test_paths=("tests/test_gateway*.py",),
            ),
            MODULE.ScopeDefinition(
                name="tools",
                source_paths=("src/assistant_agent/tools/**",),
                test_paths=("tests/test_tool_*.py",),
            ),
        ),
    )


def test_load_scope_map_and_select_explicit_scope(tmp_path: Path) -> None:
    config = tmp_path / "scope-map.toml"
    config.write_text(
        '[critical]\ntest_paths=["tests/unit"]\n'
        '[[scope]]\nname="tools"\n'
        'source_paths=["src/assistant_agent/tools/**"]\n'
        'test_paths=["tests/test_tool_*.py"]\n',
        encoding="utf-8",
    )

    selection = MODULE.select_explicit_scopes(MODULE.load_scope_map(config), ["tools"])

    assert selection.scopes == ("tools",)
    assert selection.test_paths == ("tests/unit", "tests/test_tool_*.py")


def test_explicit_scope_selection_is_stable_and_deduplicated() -> None:
    selection = MODULE.select_explicit_scopes(_scope_map(), ["tools", "gateway", "tools"])

    assert selection.scopes == ("gateway", "tools")
    assert selection.test_paths == (
        "tests/unit",
        "tests/test_gateway*.py",
        "tests/test_tool_*.py",
    )


def test_unknown_explicit_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知测试 scope: missing"):
        MODULE.select_explicit_scopes(_scope_map(), ["missing"])


def test_changed_paths_select_multiple_scopes() -> None:
    selection = MODULE.select_changed_scopes(
        _scope_map(),
        [
            "src/assistant_agent/tools/registry.py",
            "src/assistant_agent/gateway/session.py",
        ],
    )

    assert selection.scopes == ("gateway", "tools")
    assert selection.test_paths == (
        "tests/unit",
        "tests/test_gateway*.py",
        "tests/test_tool_*.py",
    )


def test_shared_test_infrastructure_selects_all_scopes() -> None:
    selection = MODULE.select_changed_scopes(_scope_map(), ["tests/conftest.py"])

    assert selection.scopes == ("gateway", "tools")


def test_documentation_change_runs_only_critical() -> None:
    selection = MODULE.select_changed_scopes(_scope_map(), ["docs/testing.md"])

    assert selection.scopes == ()
    assert selection.test_paths == ("tests/unit",)


def test_unmapped_source_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="未映射源码路径"):
        MODULE.select_changed_scopes(
            _scope_map(),
            ["src/assistant_agent/unknown/new_boundary.py"],
        )


def test_expand_test_paths_expands_directories_and_globs(tmp_path: Path) -> None:
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/test_tool_alpha.py").write_text("", encoding="utf-8")
    (tmp_path / "tests/test_tool_beta.py").write_text("", encoding="utf-8")

    expanded = MODULE.expand_test_paths(
        tmp_path,
        ("tests/unit", "tests/test_tool_*.py", "tests/test_tool_alpha.py"),
    )

    assert expanded == (
        "tests/unit",
        "tests/test_tool_alpha.py",
        "tests/test_tool_beta.py",
    )


def test_expand_test_paths_rejects_empty_pattern(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="没有匹配测试"):
        MODULE.expand_test_paths(tmp_path, ("tests/missing_*.py",))


def test_build_pytest_command_uses_selected_paths() -> None:
    command = MODULE.build_pytest_command(
        "/env/bin/python",
        ("tests/unit", "tests/test_gateway.py"),
        ["-q"],
    )

    assert command == [
        "/env/bin/python",
        "-m",
        "pytest",
        "tests/unit",
        "tests/test_gateway.py",
        "-q",
    ]


def test_offline_environment_removes_integration_opt_in() -> None:
    env = MODULE.offline_environment(
        {
            "RUN_INTEGRATION_TESTS": "1",
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "pilot",
        }
    )

    assert "RUN_INTEGRATION_TESTS" not in env
    assert env["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "mock"
    assert env["MULTIMODAL_AGENT_DISABLE_DOTENV"] == "1"


def test_changed_paths_for_range_reports_add_modify_delete_and_rename(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "src/assistant_agent/tools/old.py", "old\n")
    _write(tmp_path / "src/assistant_agent/gateway/modify.py", "before\n")
    _write(tmp_path / "src/assistant_agent/memory/delete.py", "delete\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    _git(
        tmp_path,
        "mv",
        "src/assistant_agent/tools/old.py",
        "src/assistant_agent/tools/new.py",
    )
    _write(tmp_path / "src/assistant_agent/gateway/modify.py", "after\n")
    (tmp_path / "src/assistant_agent/memory/delete.py").unlink()
    _write(tmp_path / "src/assistant_agent/api/added.py", "added\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "changes")

    paths = MODULE.changed_paths_for_range(tmp_path, f"{base}..HEAD")

    assert paths == (
        "src/assistant_agent/api/added.py",
        "src/assistant_agent/gateway/modify.py",
        "src/assistant_agent/memory/delete.py",
        "src/assistant_agent/tools/new.py",
        "src/assistant_agent/tools/old.py",
    )


def test_changed_paths_for_range_reports_invalid_range(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    with pytest.raises(ValueError, match="非法 Git range"):
        MODULE.changed_paths_for_range(tmp_path, "missing..HEAD")


def test_main_propagates_pytest_exit_code_and_forces_offline_environment(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    _write_scope_fixture(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    exit_code = MODULE.main(
        [
            "--repo-root",
            str(tmp_path),
            "--scope-map",
            "scope-map.toml",
            "--scope",
            "tools",
            "--",
            "-q",
        ]
    )

    assert exit_code == 5
    assert captured["command"][-1] == "-q"
    assert captured["env"]["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "mock"
    output = capsys.readouterr().out
    assert "mode: scoped" in output
    assert "scopes: tools" in output


def test_full_legacy_builds_explicit_offline_command(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    assert MODULE.main(
        [
            "--repo-root",
            str(tmp_path),
            "--full-legacy",
            "--",
            "--collect-only",
        ]
    ) == 0
    assert captured["command"][1:] == [
        "-m",
        "pytest",
        "tests",
        "-m",
        "not integration",
        "--collect-only",
    ]


def test_real_runner_does_not_modify_tracked_files(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_scope_fixture(tmp_path)
    _write(
        tmp_path / "tests/test_tool_sample.py",
        "def test_sample():\n    assert True\n",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "fixture")
    before = _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout

    exit_code = MODULE.main(
        [
            "--repo-root",
            str(tmp_path),
            "--scope-map",
            "scope-map.toml",
            "--scope",
            "tools",
            "--",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
    )

    after = _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout
    assert exit_code == 0
    assert after == before == ""


def test_repository_scope_map_has_required_scopes() -> None:
    scope_map = MODULE.load_scope_map(PROJECT_ROOT / "tests/scope-map.toml")

    assert scope_map.critical_paths == ("tests/unit", "tests/contracts")
    assert {scope.name for scope in scope_map.scopes} == {
        "prompt",
        "context",
        "tools",
        "gateway",
        "runtime",
        "memory",
        "providers",
        "api",
    }


def test_repository_scope_map_test_patterns_all_expand() -> None:
    scope_map = MODULE.load_scope_map(PROJECT_ROOT / "tests/scope-map.toml")

    for scope in scope_map.scopes:
        expanded = MODULE.expand_test_paths(PROJECT_ROOT, scope.test_paths)
        assert expanded, scope.name


def test_test_documentation_routes_scoped_and_legacy_commands() -> None:
    readme = (PROJECT_ROOT / "tests/README.md").read_text(encoding="utf-8")
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    skill = (
        PROJECT_ROOT / ".codex/skills/assistant-agent-test-governance/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "run_scoped_tests.py --changed" in readme
    assert "run_scoped_tests.py --full-legacy" in readme
    assert "普通开发" in agents and "run_scoped_tests.py" in agents
    assert "full-legacy" in skill


def _write_scope_fixture(root: Path) -> None:
    _write(root / "tests/unit/test_bootstrap.py", "def test_bootstrap():\n    assert True\n")
    _write(root / "tests/test_tool_sample.py", "def test_tool():\n    assert True\n")
    _write(
        root / "scope-map.toml",
        '[critical]\ntest_paths=["tests/unit"]\n'
        '[[scope]]\nname="tools"\n'
        'source_paths=["src/assistant_agent/tools/**"]\n'
        'test_paths=["tests/test_tool_*.py"]\n',
    )


def _init_git_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Tests")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
