#!/usr/bin/env python3
"""Select repository tests by stable architectural scope."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
from pathlib import Path
import subprocess
import sys
import tomllib


_SHARED_TEST_PATHS = {
    "pyproject.toml",
    "scripts/run_scoped_tests.py",
    "tests/conftest.py",
    "tests/scope-map.toml",
}


@dataclass(frozen=True)
class ScopeDefinition:
    name: str
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]


@dataclass(frozen=True)
class ScopeMap:
    critical_paths: tuple[str, ...]
    scopes: tuple[ScopeDefinition, ...]


@dataclass(frozen=True)
class TestSelection:
    scopes: tuple[str, ...]
    test_paths: tuple[str, ...]


def load_scope_map(path: Path) -> ScopeMap:
    """Load and validate the test routing authority."""

    with path.open("rb") as handle:
        payload = tomllib.load(handle)

    critical = payload.get("critical")
    if not isinstance(critical, dict):
        raise ValueError("scope map 缺少 [critical]")
    critical_paths = _required_strings(critical, "test_paths", owner="critical")

    raw_scopes = payload.get("scope", [])
    if not isinstance(raw_scopes, list):
        raise ValueError("scope map 的 [[scope]] 必须是列表")

    scopes: list[ScopeDefinition] = []
    seen_names: set[str] = set()
    for raw_scope in raw_scopes:
        if not isinstance(raw_scope, dict):
            raise ValueError("每个 [[scope]] 必须是 TOML table")
        name = str(raw_scope.get("name") or "").strip()
        if not name:
            raise ValueError("scope name 不能为空")
        if name in seen_names:
            raise ValueError(f"重复测试 scope: {name}")
        seen_names.add(name)
        scopes.append(
            ScopeDefinition(
                name=name,
                source_paths=_required_strings(raw_scope, "source_paths", owner=name),
                test_paths=_required_strings(raw_scope, "test_paths", owner=name),
            )
        )

    return ScopeMap(critical_paths=critical_paths, scopes=tuple(scopes))


def select_explicit_scopes(scope_map: ScopeMap, names: list[str]) -> TestSelection:
    """Select critical tests plus explicitly named scopes."""

    by_name = {scope.name: scope for scope in scope_map.scopes}
    selected_names = tuple(sorted(set(names)))
    unknown = [name for name in selected_names if name not in by_name]
    if unknown:
        raise ValueError(f"未知测试 scope: {', '.join(unknown)}")
    return _selection(scope_map, selected_names)


def select_changed_scopes(
    scope_map: ScopeMap,
    changed_paths: list[str],
) -> TestSelection:
    """Map changed repository paths to critical tests and affected scopes."""

    normalized = tuple(_normalize_path(path) for path in changed_paths)
    if any(_is_shared_test_path(path) for path in normalized):
        return _selection(scope_map, tuple(sorted(scope.name for scope in scope_map.scopes)))

    selected: set[str] = set()
    unmapped_source_paths: list[str] = []
    for path in normalized:
        matching = [
            scope.name
            for scope in scope_map.scopes
            if any(fnmatchcase(path, pattern) for pattern in scope.source_paths)
        ]
        selected.update(matching)
        if path.startswith("src/assistant_agent/") and not matching:
            unmapped_source_paths.append(path)

    if unmapped_source_paths:
        raise ValueError(f"未映射源码路径: {', '.join(sorted(unmapped_source_paths))}")
    return _selection(scope_map, tuple(sorted(selected)))


def expand_test_paths(repo_root: Path, patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Expand configured test paths and globs with stable de-duplication."""

    expanded: list[str] = []
    for pattern in patterns:
        matches = _matches_for_pattern(repo_root, pattern)
        if not matches:
            raise ValueError(f"测试模式没有匹配测试: {pattern}")
        expanded.extend(matches)
    return tuple(dict.fromkeys(expanded))


def changed_paths_for_range(repo_root: Path, git_range: str) -> tuple[str, ...]:
    """Return all paths affected by a Git range, including both rename sides."""

    completed = subprocess.run(
        ["git", "diff", "--name-status", "-M", git_range],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"非法 Git range: {git_range}: {detail}")

    paths: set[str] = set()
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            paths.update((_normalize_path(fields[1]), _normalize_path(fields[2])))
        else:
            paths.add(_normalize_path(fields[1]))
    return tuple(sorted(path for path in paths if path))


def build_pytest_command(
    python: str,
    test_paths: tuple[str, ...],
    extra_args: list[str],
) -> list[str]:
    """Build a direct pytest command without invoking a shell."""

    return [python, "-m", "pytest", *test_paths, *extra_args]


def offline_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Force deterministic local execution and remove integration opt-in."""

    env = dict(base)
    env.pop("RUN_INTEGRATION_TESTS", None)
    env["MULTIMODAL_AGENT_RUNTIME_PROFILE"] = "mock"
    env["MULTIMODAL_AGENT_DISABLE_DOTENV"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--scope-map", type=Path, default=Path("tests/scope-map.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scope", action="append", dest="scopes", metavar="NAME")
    mode.add_argument("--changed", metavar="BASE..HEAD")
    mode.add_argument("--full", action="store_true")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Select tests, print the decision, and propagate pytest's exit code."""

    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    extra_args = list(args.pytest_args)
    if extra_args[:1] == ["--"]:
        extra_args = extra_args[1:]

    try:
        if args.full:
            mode = "full"
            scopes: tuple[str, ...] = ()
            test_paths = ("tests/critical", "tests/scopes")
            pytest_args = ["-m", "not integration", *extra_args]
        else:
            scope_map_path = args.scope_map
            if not scope_map_path.is_absolute():
                scope_map_path = repo_root / scope_map_path
            scope_map = load_scope_map(scope_map_path)
            if args.changed:
                changed_paths = list(changed_paths_for_range(repo_root, args.changed))
                selection = select_changed_scopes(scope_map, changed_paths)
                mode = "changed"
            else:
                selection = select_explicit_scopes(scope_map, args.scopes or [])
                mode = "scoped"
            scopes = selection.scopes
            test_paths = expand_test_paths(repo_root, selection.test_paths)
            pytest_args = extra_args
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"test routing error: {exc}", file=sys.stderr)
        return 2

    print(f"mode: {mode}")
    if mode == "full":
        scope_label = "(all scopes)"
    else:
        scope_label = ", ".join(scopes) if scopes else "(critical only)"
    print(f"scopes: {scope_label}")
    print("test_paths:")
    for path in test_paths:
        print(f"  - {path}")

    command = build_pytest_command(sys.executable, test_paths, pytest_args)
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=offline_environment(os.environ),
    )
    return completed.returncode


def _selection(scope_map: ScopeMap, selected_names: tuple[str, ...]) -> TestSelection:
    by_name = {scope.name: scope for scope in scope_map.scopes}
    paths = list(scope_map.critical_paths)
    for name in selected_names:
        paths.extend(by_name[name].test_paths)
    return TestSelection(scopes=selected_names, test_paths=tuple(dict.fromkeys(paths)))


def _required_strings(payload: dict, key: str, *, owner: str) -> tuple[str, ...]:
    values = payload.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{owner}.{key} 必须是非空字符串列表")
    normalized = tuple(str(value).strip() for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"{owner}.{key} 不能包含空值")
    return normalized


def _normalize_path(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")


def _is_shared_test_path(path: str) -> bool:
    return path in _SHARED_TEST_PATHS or path.startswith("tests/")


def _matches_for_pattern(repo_root: Path, pattern: str) -> list[str]:
    normalized = _normalize_path(pattern)
    if not any(character in normalized for character in "*?["):
        candidate = repo_root / normalized
        return [normalized] if candidate.exists() else []
    return sorted(
        match.relative_to(repo_root).as_posix()
        for match in repo_root.glob(normalized)
        if match.exists()
    )


if __name__ == "__main__":
    raise SystemExit(main())
