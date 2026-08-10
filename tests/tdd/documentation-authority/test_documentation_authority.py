from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import check_documentation_authority as authority


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIELDS = (
    "定位",
    "Owns",
    "Does not own",
    "源码与 schema 入口",
    "验证入口",
    "相邻 authority",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path, *, manifest: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "src/example").mkdir(parents=True)
    (repo / "docs/authority.toml").write_text(manifest, encoding="utf-8")
    (repo / "docs/example.md").write_text(
        """# Example

## Authority contract

| field | value |
| --- | --- |
| 定位 | example |
| Owns | example |
| Does not own | other |
| 源码与 schema 入口 | `src/example/` |
| 验证入口 | manifest |
| 相邻 authority | none |
""",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "Read `docs/example.md` for example work.\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    (repo / "src/example/module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _domain(*, domain_id: str = "example") -> str:
    return f'''[[domains]]
id = "{domain_id}"
authority = "docs/example.md"
read_when = ["example work"]
source_globs = ["src/example/**"]
thin_references = ["README.md"]
verification = ["python scripts/check_documentation_authority.py --repo-root ."]
exclusive_literals = ["EXAMPLE_ONLY_LITERAL"]
exclusive_allowlist = []
'''


def _manifest(*domains: str, schema_version: int = 1, coverage: str = "pilot") -> str:
    return (
        f'schema_version = {schema_version}\ncoverage = "{coverage}"\n\n'
        + "\n".join(domains)
    )


def _codes(report: authority.ValidationReport) -> set[str]:
    return {item.code for item in report.errors}


def test_manifest_loads_a_typed_domain(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))

    manifest = authority.AuthorityManifest.load(repo)

    assert manifest.schema_version == 1
    assert manifest.coverage == "pilot"
    assert manifest.domains == (
        authority.AuthorityDomain(
            id="example",
            authority="docs/example.md",
            read_when=("example work",),
            source_globs=("src/example/**",),
            thin_references=("README.md",),
            verification=("python scripts/check_documentation_authority.py --repo-root .",),
            exclusive_literals=("EXAMPLE_ONLY_LITERAL",),
            exclusive_allowlist=(),
        ),
    )


def test_duplicate_domain_ids_are_reported_structurally(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path,
        manifest=_manifest(_domain(), _domain()),
    )

    report = authority.validate_repository(repo)

    assert "duplicate_domain_id" in _codes(report)
    assert report.valid is False


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path,
        manifest=_manifest(_domain(), schema_version=2),
    )

    report = authority.validate_repository(repo)

    assert _codes(report) == {"unsupported_schema_version"}


def test_manifest_rejects_wrong_field_types(tmp_path: Path) -> None:
    invalid_domain = _domain().replace(
        'read_when = ["example work"]',
        'read_when = "example work"',
    )
    repo = _repository(tmp_path, manifest=_manifest(invalid_domain))

    report = authority.validate_repository(repo)

    assert "invalid_manifest" in _codes(report)


def test_manifest_rejects_repository_escape_paths(tmp_path: Path) -> None:
    invalid_domain = _domain().replace(
        'authority = "docs/example.md"',
        'authority = "../outside.md"',
    )
    repo = _repository(tmp_path, manifest=_manifest(invalid_domain))

    report = authority.validate_repository(repo)

    assert "invalid_manifest_path" in _codes(report)


def test_manifest_rejects_repository_escape_source_globs(tmp_path: Path) -> None:
    invalid_domain = _domain().replace(
        'source_globs = ["src/example/**"]',
        'source_globs = ["../outside/**"]',
    )
    repo = _repository(tmp_path, manifest=_manifest(invalid_domain))

    report = authority.validate_repository(repo)

    assert "invalid_manifest_path" in _codes(report)


def test_missing_authority_and_thin_reference_are_reported(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "docs/example.md").unlink()
    (repo / "README.md").unlink()

    report = authority.validate_repository(repo)

    assert _codes(report) == {"missing_authority", "missing_thin_reference"}


def test_manifest_authority_must_be_routed_by_agents(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "AGENTS.md").write_text("# No domain route\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert _codes(report) == {"authority_not_routed"}


def test_exclusive_literal_is_rejected_in_a_thin_reference(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "README.md").write_text("EXAMPLE_ONLY_LITERAL\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    leaks = [item for item in report.errors if item.code == "exclusive_literal_leak"]
    assert [(item.domain_id, item.path) for item in leaks] == [
        ("example", "README.md")
    ]


def test_exclusive_literal_is_rejected_in_an_unregistered_root_authority(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "docs/other-current.md").write_text(
        "EXAMPLE_ONLY_LITERAL\n",
        encoding="utf-8",
    )

    report = authority.validate_repository(repo)

    leaks = [item for item in report.errors if item.code == "exclusive_literal_leak"]
    assert [(item.domain_id, item.path) for item in leaks] == [
        ("example", "docs/other-current.md")
    ]


def test_exclusive_literal_allows_owner_history_and_exact_allowlist(
    tmp_path: Path,
) -> None:
    allowed_domain = _domain().replace(
        "exclusive_allowlist = []",
        'exclusive_allowlist = ["README.md"]',
    )
    repo = _repository(tmp_path, manifest=_manifest(allowed_domain))
    owner = repo / "docs/example.md"
    owner.write_text(
        owner.read_text(encoding="utf-8") + "\nEXAMPLE_ONLY_LITERAL\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("EXAMPLE_ONLY_LITERAL\n", encoding="utf-8")
    history = repo / "docs/superpowers/specs/history.md"
    history.parent.mkdir(parents=True)
    history.write_text("EXAMPLE_ONLY_LITERAL\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert report.valid is True


def test_exclusive_allowlist_rejects_globs(tmp_path: Path) -> None:
    invalid_domain = _domain().replace(
        "exclusive_allowlist = []",
        'exclusive_allowlist = ["docs/**"]',
    )
    repo = _repository(tmp_path, manifest=_manifest(invalid_domain))

    report = authority.validate_repository(repo)

    assert "invalid_manifest_path" in _codes(report)


def test_unmatched_source_glob_is_reported(tmp_path: Path) -> None:
    unmatched_domain = _domain().replace(
        'source_globs = ["src/example/**"]',
        'source_globs = ["src/missing/**"]',
    )
    repo = _repository(tmp_path, manifest=_manifest(unmatched_domain))

    report = authority.validate_repository(repo)

    assert _codes(report) == {"unmatched_source_glob"}


def test_dirty_source_path_selects_review_domain(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "src/example/module.py").write_text("VALUE = 2\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert report.valid is True
    assert report.review_required == ("example",)


def test_git_range_review_does_not_mix_in_dirty_paths(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "src/example/module.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "src/example/module.py")
    _git(repo, "commit", "-qm", "change source")
    (repo / "README.md").write_text("dirty but outside source globs\n", encoding="utf-8")

    report = authority.validate_repository(repo, git_range="HEAD~1..HEAD")

    assert report.valid is True
    assert report.review_required == ("example",)


def test_complete_coverage_rejects_unregistered_current_authority_route(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        manifest=_manifest(_domain(), coverage="complete"),
    )
    (repo / "docs/second.md").write_text("# Second\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Read `docs/authority.toml`.\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert _codes(report) == {"unregistered_current_authority"}


def test_complete_coverage_routes_through_manifest_not_each_authority(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        manifest=_manifest(_domain(), coverage="complete"),
    )
    (repo / "AGENTS.md").write_text("Read `docs/authority.toml`.\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert report.valid is True


def test_complete_coverage_requires_manifest_route(tmp_path: Path) -> None:
    repo = _repository(
        tmp_path,
        manifest=_manifest(_domain(), coverage="complete"),
    )
    (repo / "AGENTS.md").write_text("# No manifest route\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert _codes(report) == {"manifest_not_routed"}


def test_complete_coverage_rejects_non_current_authority(tmp_path: Path) -> None:
    domain = _domain().replace(
        'authority = "docs/example.md"',
        'authority = "docs/reference/nested.md"',
    )
    repo = _repository(tmp_path, manifest=_manifest(domain, coverage="complete"))
    nested = repo / "docs/reference/nested.md"
    nested.parent.mkdir(parents=True)
    nested.write_text((repo / "docs/example.md").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "AGENTS.md").write_text("Read `docs/authority.toml`.\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert _codes(report) == {
        "unregistered_current_authority",
        "unknown_manifest_authority",
    }


def test_authority_requires_contract_heading(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    (repo / "docs/example.md").write_text("# Example\n", encoding="utf-8")

    report = authority.validate_repository(repo)

    assert _codes(report) == {"missing_authority_contract"}


def test_authority_requires_every_contract_field(tmp_path: Path) -> None:
    repo = _repository(tmp_path, manifest=_manifest(_domain()))
    path = repo / "docs/example.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("| Does not own | other |\n", ""),
        encoding="utf-8",
    )

    report = authority.validate_repository(repo)

    issues = [item for item in report.errors if item.code == "missing_authority_contract_field"]
    assert [(item.domain_id, item.path) for item in issues] == [
        ("example", "docs/example.md")
    ]
    assert "Does not own" in issues[0].message


def test_repository_manifest_and_authority_contract_cards_are_valid() -> None:
    manifest = authority.AuthorityManifest.load(REPO_ROOT)
    report = authority.validate_repository(REPO_ROOT)

    assert report.valid is True
    assert {domain.id for domain in manifest.domains} == {
        "agent-communication",
        "agent-eval",
        "context-engineering",
        "gateway",
        "media-agent-protocol",
        "memory-plugin",
        "memory-server-api",
        "multimodal-embedding",
        "observability-diagnosis",
        "runtime-event-stream",
        "runtime-observability",
        "test-policy",
        "tool-calling",
    }
    for domain in manifest.domains:
        markdown = (REPO_ROOT / domain.authority).read_text(encoding="utf-8")
        assert "## Authority contract" in markdown
        for field in CONTRACT_FIELDS:
            assert f"| {field} |" in markdown
