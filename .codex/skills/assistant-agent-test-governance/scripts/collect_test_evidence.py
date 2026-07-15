#!/usr/bin/env python3
"""Collect deterministic assistant_agent test-governance evidence as JSON."""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable


SCHEMA_VERSION = 1
REFERENCE_ROOTS = ("docs", "scripts")


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo_root)


def _is_git_repository(repo_root: Path) -> bool:
    result = _git(repo_root, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _test_paths(repo_root: Path) -> list[Path]:
    tests_root = repo_root / "tests"
    if not tests_root.is_dir():
        return []
    return sorted(
        path
        for path in tests_root.rglob("test*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _imports_and_targets(path: Path) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=os.fspath(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return [], []

    imports: set[str] = set()
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                qualified = ".".join(part for part in (module, alias.name) if part)
                imports.add(qualified)
                aliases[alias.asname or alias.name] = qualified

    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in aliases:
            targets.add(aliases[node.func.id])
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            root = aliases.get(node.func.value.id)
            if root:
                targets.add(f"{root}.{node.func.attr}")
    return sorted(imports), sorted(targets)


class _NormalizeTestFunction(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        node.name = "test"
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:  # noqa: N802
        node.name = "test"
        return self.generic_visit(node)


class _TestFunctionFingerprintCollector(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.class_names: list[str] = []
        self.result: dict[str, str] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.class_names.append(node.name)
        self.generic_visit(node)
        self.class_names.pop()

    def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not node.name.startswith("test"):
            return
        normalized = _NormalizeTestFunction().visit(
            ast.fix_missing_locations(ast.parse(ast.unparse(node)))
        ).body[0]
        dumped = ast.dump(normalized, annotate_fields=True, include_attributes=False)
        nodeid = "::".join((self.relative, *self.class_names, node.name))
        self.result[nodeid] = hashlib.sha256(dumped.encode("utf-8")).hexdigest()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._record(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._record(node)


def _function_fingerprints(path: Path, relative: str) -> dict[str, str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    collector = _TestFunctionFingerprintCollector(relative)
    collector.visit(tree)
    return collector.result


def _last_touch(repo_root: Path, relative: str) -> dict[str, str] | None:
    result = _git(repo_root, "log", "-1", "--format=%H%x00%aI", "--", relative)
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    commit, _, timestamp = value.partition("\x00")
    return {"commit": commit, "timestamp": timestamp}


def _inbound_references(repo_root: Path, relative: str) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    needles = {relative, Path(relative).name}
    for root_name in REFERENCE_ROOTS:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for source in sorted(path for path in root.rglob("*") if path.is_file()):
            try:
                lines = source.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if any(needle in line for needle in needles):
                    references.append(
                        {
                            "source": source.relative_to(repo_root).as_posix(),
                            "line": line_number,
                        }
                    )
    return sorted(references, key=lambda item: (item["source"], item["line"]))


def _git_changes(repo_root: Path, git_range: str | None) -> tuple[dict[str, Any], dict[str, str] | None]:
    if not _is_git_repository(repo_root):
        return {"available": False, "changes": [], "range": git_range}, None
    if not git_range:
        return {"available": True, "changes": [], "range": None}, None
    verification = _git(repo_root, "rev-list", "--max-count=1", git_range)
    if verification.returncode != 0:
        return (
            {"available": True, "changes": [], "range": git_range},
            {
                "code": "invalid_git_range",
                "message": f"Git range is not valid: {git_range}",
            },
        )
    diff = _git(repo_root, "diff", "--name-status", "--find-renames", git_range)
    if diff.returncode != 0:
        return (
            {"available": True, "changes": [], "range": git_range},
            {"code": "git_diff_failed", "message": diff.stderr.strip() or "git diff failed"},
        )
    changes: list[dict[str, str]] = []
    for line in diff.stdout.splitlines():
        fields = line.split("\t")
        status_code = fields[0]
        status = status_code[0]
        if status in {"R", "C"} and len(fields) >= 3:
            changes.append({"status": status, "old_path": fields[1], "path": fields[2]})
        elif len(fields) >= 2:
            changes.append({"status": status, "path": fields[1]})
    changes.sort(key=lambda item: (item["path"], item["status"], item.get("old_path", "")))
    return {"available": True, "changes": changes, "range": git_range}, None


@dataclass(eq=False)
class _PytestResultPlugin:
    collect: bool

    def __post_init__(self) -> None:
        self.tests: list[dict[str, Any]] = []
        self.reports: dict[str, dict[str, Any]] = {}

    def pytest_collection_finish(self, session: Any) -> None:
        self.tests = [
            {
                "nodeid": item.nodeid,
                "path": Path(str(item.path)).relative_to(Path(str(session.config.rootpath))).as_posix(),
                "markers": sorted({marker.name for marker in item.iter_markers()}),
            }
            for item in session.items
        ]

    def pytest_runtest_logreport(self, report: Any) -> None:
        record = self.reports.setdefault(
            report.nodeid,
            {"nodeid": report.nodeid, "duration_seconds": 0.0, "outcome": "passed"},
        )
        record["duration_seconds"] += float(report.duration)
        was_xfail = getattr(report, "wasxfail", None)
        if report.failed:
            record["outcome"] = "failed"
        elif report.skipped:
            record["outcome"] = "xfailed" if was_xfail else "skipped"
        elif report.when == "call":
            record["outcome"] = "xpassed" if was_xfail else "passed"


def _pytest_worker(args: argparse.Namespace) -> int:
    import pytest

    sys.path.insert(0, os.fspath(args.repo_root.resolve()))
    plugin = _PytestResultPlugin(collect=args.worker_mode == "collect")
    pytest_args = ["--rootdir", os.fspath(args.repo_root), "-p", "no:cacheprovider", "-q"]
    if args.worker_mode == "collect":
        pytest_args.append("--collect-only")
    elif args.selection:
        pytest_args.extend(["-m", args.selection])
    pytest_args.append("tests")
    exit_code = int(pytest.main(pytest_args, plugins=[plugin]))
    output = {
        "pytest_exit_code": exit_code,
        "tests": sorted(plugin.tests, key=lambda item: item["nodeid"]),
        "results": sorted(plugin.reports.values(), key=lambda item: item["nodeid"]),
    }
    Path(args.worker_output).write_text(json.dumps(output, sort_keys=True), encoding="utf-8")
    return 0


def _run_pytest_worker(repo_root: Path, *, mode: str, selection: str | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="assistant-agent-test-evidence-") as temp_dir:
        output_path = Path(temp_dir) / "pytest.json"
        command = [
            sys.executable,
            os.fspath(Path(__file__).resolve()),
            "--repo-root",
            os.fspath(repo_root),
            "--worker-mode",
            mode,
            "--worker-output",
            os.fspath(output_path),
        ]
        if selection:
            command.extend(["--selection", selection])
        environment = os.environ.copy()
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["MULTIMODAL_AGENT_RUNTIME_PROFILE"] = "mock"
        environment.pop("RUN_INTEGRATION_TESTS", None)
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if completed.returncode != 0 or not output_path.exists():
            return {
                "pytest_exit_code": completed.returncode,
                "tests": [],
                "results": [],
                "worker_error": completed.stderr.strip() or completed.stdout.strip(),
            }
        return json.loads(output_path.read_text(encoding="utf-8"))


def _duplicates(test_paths: Iterable[Path], repo_root: Path, tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fingerprints: dict[str, list[str]] = defaultdict(list)
    for path in test_paths:
        relative = path.relative_to(repo_root).as_posix()
        for base_nodeid, fingerprint in _function_fingerprints(path, relative).items():
            fingerprints[fingerprint].append(base_nodeid)

    duplicates: list[dict[str, Any]] = []
    for fingerprint, base_nodeids in fingerprints.items():
        distinct = sorted(set(base_nodeids))
        if len(distinct) < 2:
            continue
        matched_bases = [
            base
            for base in distinct
            if any(item["nodeid"] == base or item["nodeid"].startswith(f"{base}[") for item in tests)
        ]
        if len(matched_bases) < 2:
            continue
        nodeids = sorted(
            item["nodeid"]
            for item in tests
            if any(item["nodeid"] == base or item["nodeid"].startswith(f"{base}[") for base in matched_bases)
        )
        duplicates.append({"fingerprint": fingerprint, "nodeids": nodeids})
    return sorted(duplicates, key=lambda item: item["nodeids"])


def collect(repo_root: Path, git_range: str | None, profile: str) -> tuple[dict[str, Any], int]:
    repo_root = repo_root.resolve()
    git, git_error = _git_changes(repo_root, git_range)
    collection = _run_pytest_worker(repo_root, mode="collect")
    tests = collection["tests"]
    paths = _test_paths(repo_root)
    git_available = bool(git["available"])
    test_files = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        imports, targets = _imports_and_targets(path)
        test_files.append(
            {
                "path": relative,
                "imports": imports,
                "targets": targets,
                "last_touch": _last_touch(repo_root, relative) if git_available else None,
                "inbound_references": _inbound_references(repo_root, relative),
            }
        )

    errors = [git_error] if git_error else []
    if collection.get("worker_error"):
        errors.append({"code": "pytest_collection_failed", "message": collection["worker_error"]})
    elif collection["pytest_exit_code"] not in {0, 5}:
        errors.append(
            {
                "code": "pytest_collection_failed",
                "message": f"pytest collection exited with {collection['pytest_exit_code']}",
            }
        )

    profile_run: dict[str, Any] | None = None
    if profile != "none" and not any(error["code"] == "invalid_git_range" for error in errors):
        selection = "fast" if profile == "fast" else "not integration"
        run = _run_pytest_worker(repo_root, mode="run", selection=selection)
        results = run["results"]
        profile_run = {
            "profile": profile,
            "selection": selection,
            "pytest_exit_code": run["pytest_exit_code"],
            "counts": dict(sorted(Counter(item["outcome"] for item in results).items())),
            "duration_seconds": round(sum(item["duration_seconds"] for item in results), 6),
            "results": results,
        }
        if run.get("worker_error"):
            errors.append({"code": "pytest_profile_failed", "message": run["worker_error"]})

    payload = {
        "schema_version": SCHEMA_VERSION,
        "repository": {"root": os.fspath(repo_root)},
        "profile": profile,
        "git": git,
        "test_files": test_files,
        "tests": tests,
        "duplicates": _duplicates(paths, repo_root, tests),
        "profile_run": profile_run,
        "errors": errors,
    }
    return payload, 2 if git_error else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--git-range")
    parser.add_argument("--profile", choices=("none", "fast", "full-offline"), default="none")
    parser.add_argument("--worker-mode", choices=("collect", "run"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", help=argparse.SUPPRESS)
    parser.add_argument("--selection", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker_mode:
        return _pytest_worker(args)
    try:
        payload, exit_code = collect(args.repo_root, args.git_range, args.profile)
    except Exception as exc:  # Keep the stdout contract even for unexpected errors.
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repository": {"root": os.fspath(args.repo_root.resolve())},
            "profile": args.profile,
            "git": {"available": False, "changes": [], "range": args.git_range},
            "test_files": [],
            "tests": [],
            "duplicates": [],
            "profile_run": None,
            "errors": [{"code": "collector_error", "message": str(exc)}],
        }
        exit_code = 2
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
