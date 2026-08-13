from __future__ import annotations

import importlib.util
import re
from pathlib import Path

def repository_matches(
    repo_root: Path,
    pattern: str,
    *,
    roots: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> list[str]:
    matcher = re.compile(pattern, re.IGNORECASE)
    matches: list[str] = []
    for root_name in roots:
        root = repo_root / root_name
        paths = root.rglob("*") if root.is_dir() else (root,)
        for path in paths:
            if not path.is_file():
                continue
            relative = path.relative_to(repo_root).as_posix()
            if any(relative == item or relative.startswith(f"{item}/") for item in exclude):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if matcher.search(line):
                    matches.append(f"{relative}:{line_number}:{line.strip()}")
    return matches


def test_production_roots_have_no_retired_platform_surface() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    forbidden = repository_matches(
        repo_root,
        r"lang" + r"fuse|ASSISTANT_AGENT_LANG" + r"FUSE|LANG" + r"FUSE_",
        roots=(
            "src",
            "evals",
            "scripts",
            "deploy",
            "docs",
            "README.md",
            ".env.example",
            "pyproject.toml",
        ),
        exclude=("docs/development", "docs/superpowers"),
    )
    assert forbidden == []


def test_workflow_store_has_no_shadow_trace_observer() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source = (repo_root / "src/assistant_agent/runtime/runtime.py").read_text(
        encoding="utf-8"
    )
    assert "ObservedWorkflowStore" not in source
    assert "create_workflow_otel_observer_from_env" not in source


def test_runtime_audit_surface_is_removed() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    assert importlib.util.find_spec(
        "assistant_agent.observability.runtime_audit"
    ) is None
    assert not (repo_root / "scripts" / "run_runtime_audit.py").exists()
    assert not (
        repo_root
        / "deploy"
        / "systemd"
        / "user"
        / "assistant-agent-runtime-audit.service"
    ).exists()
    assert not (
        repo_root
        / "deploy"
        / "systemd"
        / "user"
        / "assistant-agent-runtime-audit.timer"
    ).exists()


def test_otel_export_surface_is_removed() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    for module_name in (
        "assistant_agent.observability.otel_exporter",
        "assistant_agent.observability.otel_mapping",
    ):
        assert importlib.util.find_spec(module_name) is None

    forbidden = repository_matches(
        repo_root,
        r"\botel\b|\botlp\b|opentelemetry|ASSISTANT_AGENT_OTEL|OTEL_EXPORTER_",
        roots=(
            "src",
            "evals",
            "scripts",
            "deploy",
            "docs",
            "README.md",
            "AGENTS.md",
            ".env.example",
            "pyproject.toml",
        ),
        exclude=("docs/development", "docs/superpowers"),
    )
    assert forbidden == []
