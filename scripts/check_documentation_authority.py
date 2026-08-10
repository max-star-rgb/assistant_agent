#!/usr/bin/env python3
"""Validate the repository's Agent-facing documentation authority manifest."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib
from typing import Any


SUPPORTED_SCHEMA_VERSION = 1
MANIFEST_PATH = Path("docs/authority.toml")
DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
TOP_LEVEL_FIELDS = frozenset({"schema_version", "coverage", "domains"})
DOMAIN_FIELDS = frozenset(
    {
        "id",
        "authority",
        "read_when",
        "source_globs",
        "thin_references",
        "verification",
        "exclusive_literals",
        "exclusive_allowlist",
    }
)


@dataclass(frozen=True)
class AuthorityDomain:
    id: str
    authority: str
    read_when: tuple[str, ...]
    source_globs: tuple[str, ...]
    thin_references: tuple[str, ...]
    verification: tuple[str, ...]
    exclusive_literals: tuple[str, ...]
    exclusive_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class AuthorityManifest:
    schema_version: int
    coverage: str
    domains: tuple[AuthorityDomain, ...]

    @classmethod
    def load(
        cls,
        repo_root: Path,
        manifest_path: Path | None = None,
    ) -> AuthorityManifest:
        repo = repo_root.resolve()
        path = manifest_path or repo / MANIFEST_PATH
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ManifestValidationError("invalid_manifest", str(exc)) from exc
        if not isinstance(payload, dict):
            raise ManifestValidationError("invalid_manifest", "Manifest root must be a table.")
        unknown = sorted(set(payload) - TOP_LEVEL_FIELDS)
        if unknown:
            raise ManifestValidationError(
                "invalid_manifest",
                f"Unknown manifest fields: {', '.join(unknown)}.",
            )
        schema_version = payload.get("schema_version")
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ManifestValidationError(
                "unsupported_schema_version",
                f"Unsupported authority schema version: {schema_version!r}.",
            )
        coverage = payload.get("coverage")
        if coverage not in {"pilot", "complete"}:
            raise ManifestValidationError(
                "invalid_manifest",
                "Manifest coverage must be 'pilot' or 'complete'.",
            )
        raw_domains = payload.get("domains")
        if not isinstance(raw_domains, list) or not raw_domains:
            raise ManifestValidationError(
                "invalid_manifest",
                "Manifest domains must be a non-empty array of tables.",
            )
        domains = tuple(_parse_domain(item) for item in raw_domains)
        duplicate = _first_duplicate(item.id for item in domains)
        if duplicate is not None:
            raise ManifestValidationError(
                "duplicate_domain_id",
                f"Duplicate authority domain id: {duplicate!r}.",
                domain_id=duplicate,
            )
        duplicate_authority = _first_duplicate(item.authority for item in domains)
        if duplicate_authority is not None:
            raise ManifestValidationError(
                "duplicate_authority",
                f"Authority is owned by more than one domain: {duplicate_authority!r}.",
                path=duplicate_authority,
            )
        literal_owners: dict[str, str] = {}
        for domain in domains:
            for value in (
                domain.authority,
                *domain.thin_references,
                *domain.exclusive_allowlist,
            ):
                _validate_repository_path(value, domain_id=domain.id)
            for pattern in domain.source_globs:
                _validate_source_glob(pattern, domain_id=domain.id)
            for literal in domain.exclusive_literals:
                owner = literal_owners.setdefault(literal, domain.id)
                if owner != domain.id:
                    raise ManifestValidationError(
                        "duplicate_exclusive_literal",
                        f"Exclusive literal {literal!r} has multiple owners.",
                        domain_id=domain.id,
                    )
        return cls(
            schema_version=schema_version,
            coverage=coverage,
            domains=domains,
        )


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    domain_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    schema_version: int
    valid: bool
    errors: tuple[ValidationIssue, ...]
    review_required: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "valid": self.valid,
            "errors": [asdict(item) for item in self.errors],
            "review_required": list(self.review_required),
        }


class ManifestValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        domain_id: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.issue = ValidationIssue(
            code=code,
            message=message,
            domain_id=domain_id,
            path=path,
        )


def validate_repository(
    repo_root: Path,
    *,
    git_range: str | None = None,
) -> ValidationReport:
    repo = repo_root.resolve()
    try:
        manifest = AuthorityManifest.load(repo)
    except ManifestValidationError as exc:
        return ValidationReport(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            valid=False,
            errors=(exc.issue,),
            review_required=(),
        )
    repository_paths = _repository_paths(repo)
    errors: list[ValidationIssue] = []
    agents_path = repo / "AGENTS.md"
    agents_text = _read_text(agents_path)
    current_documents = _current_document_paths(repo, manifest)
    for domain in manifest.domains:
        authority_path = repo / domain.authority
        if not authority_path.is_file():
            errors.append(
                ValidationIssue(
                    code="missing_authority",
                    message=f"Authority does not exist: {domain.authority}.",
                    domain_id=domain.id,
                    path=domain.authority,
                )
            )
        for reference in domain.thin_references:
            if not (repo / reference).is_file():
                errors.append(
                    ValidationIssue(
                        code="missing_thin_reference",
                        message=f"Thin reference does not exist: {reference}.",
                        domain_id=domain.id,
                        path=reference,
                    )
                )
        if domain.authority not in agents_text:
            errors.append(
                ValidationIssue(
                    code="authority_not_routed",
                    message=f"AGENTS.md does not route to {domain.authority}.",
                    domain_id=domain.id,
                    path=domain.authority,
                )
            )
        for pattern in domain.source_globs:
            if not any(fnmatchcase(path, pattern) for path in repository_paths):
                errors.append(
                    ValidationIssue(
                        code="unmatched_source_glob",
                        message=f"Source glob matches no repository path: {pattern}.",
                        domain_id=domain.id,
                        path=pattern,
                    )
                )
        allowed_literal_paths = {domain.authority, *domain.exclusive_allowlist}
        for document in current_documents:
            relative = document.relative_to(repo).as_posix()
            if relative in allowed_literal_paths or not document.is_file():
                continue
            content = _read_text(document)
            for literal in domain.exclusive_literals:
                if literal in content:
                    errors.append(
                        ValidationIssue(
                            code="exclusive_literal_leak",
                            message=(
                                f"Exclusive literal owned by {domain.authority} "
                                f"also appears in {relative}."
                            ),
                            domain_id=domain.id,
                            path=relative,
                        )
                    )
    if manifest.coverage == "complete":
        registered = {domain.authority for domain in manifest.domains}
        routed = set(
            re.findall(
                r"`((?:docs/[^/`]+\.md|evals/README\.md|tests/README\.md))`",
                agents_text,
            )
        )
        for path in sorted(routed - registered):
            errors.append(
                ValidationIssue(
                    code="unregistered_authority_route",
                    message=f"AGENTS.md routes an unregistered authority: {path}.",
                    path=path,
                )
            )
    changed_paths = _changed_paths(repo, git_range=git_range)
    review_required = tuple(
        sorted(
            domain.id
            for domain in manifest.domains
            if any(
                fnmatchcase(path, pattern)
                for path in changed_paths
                for pattern in domain.source_globs
            )
        )
    )
    ordered_errors = tuple(
        sorted(
            errors,
            key=lambda item: (
                item.code,
                item.domain_id or "",
                item.path or "",
                item.message,
            ),
        )
    )
    return ValidationReport(
        schema_version=manifest.schema_version,
        valid=not ordered_errors,
        errors=ordered_errors,
        review_required=review_required,
    )


def _parse_domain(value: object) -> AuthorityDomain:
    if not isinstance(value, dict):
        raise ManifestValidationError(
            "invalid_manifest",
            "Every authority domain must be a table.",
        )
    unknown = sorted(set(value) - DOMAIN_FIELDS)
    missing = sorted(DOMAIN_FIELDS - set(value))
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        if missing:
            details.append(f"missing={','.join(missing)}")
        raise ManifestValidationError(
            "invalid_manifest",
            f"Invalid authority domain fields ({'; '.join(details)}).",
        )
    domain_id = _required_string(value["id"], field="id")
    if DOMAIN_ID_RE.fullmatch(domain_id) is None:
        raise ManifestValidationError(
            "invalid_manifest",
            f"Invalid authority domain id: {domain_id!r}.",
            domain_id=domain_id,
        )
    return AuthorityDomain(
        id=domain_id,
        authority=_required_string(value["authority"], field="authority"),
        read_when=_string_tuple(value["read_when"], field="read_when", nonempty=True),
        source_globs=_string_tuple(
            value["source_globs"], field="source_globs", nonempty=True
        ),
        thin_references=_string_tuple(
            value["thin_references"], field="thin_references"
        ),
        verification=_string_tuple(
            value["verification"], field="verification", nonempty=True
        ),
        exclusive_literals=_string_tuple(
            value["exclusive_literals"], field="exclusive_literals"
        ),
        exclusive_allowlist=_string_tuple(
            value["exclusive_allowlist"], field="exclusive_allowlist"
        ),
    )


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(
            "invalid_manifest",
            f"Manifest field {field!r} must be a non-empty string.",
        )
    return value


def _string_tuple(
    value: object,
    *,
    field: str,
    nonempty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ManifestValidationError(
            "invalid_manifest",
            f"Manifest field {field!r} must be an array of non-empty strings.",
        )
    if nonempty and not value:
        raise ManifestValidationError(
            "invalid_manifest",
            f"Manifest field {field!r} must not be empty.",
        )
    return tuple(value)


def _validate_repository_path(value: str, *, domain_id: str) -> None:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or value in {"", "."}
        or any(character in value for character in "*?[")
    ):
        raise ManifestValidationError(
            "invalid_manifest_path",
            f"Authority path must stay within the repository: {value!r}.",
            domain_id=domain_id,
            path=value,
        )


def _validate_source_glob(value: str, *, domain_id: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise ManifestValidationError(
            "invalid_manifest_path",
            f"Source glob must stay within the repository: {value!r}.",
            domain_id=domain_id,
            path=value,
        )


def _first_duplicate(values) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Git command failed.")
    return result.stdout


def _repository_paths(repo: Path) -> tuple[str, ...]:
    output = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard")
    return tuple(sorted({line for line in output.splitlines() if line}))


def _changed_paths(repo: Path, *, git_range: str | None) -> tuple[str, ...]:
    if git_range is not None:
        output = _git(
            repo,
            "diff",
            "--name-only",
            "--find-renames",
            "--end-of-options",
            git_range,
            "--",
        )
        return tuple(sorted({line for line in output.splitlines() if line}))
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--"),
        ("diff", "--cached", "--name-only", "--"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        paths.update(line for line in _git(repo, *args).splitlines() if line)
    return tuple(sorted(paths))


def _current_document_paths(
    repo: Path,
    manifest: AuthorityManifest,
) -> tuple[Path, ...]:
    relative_paths = {
        "AGENTS.md",
        "README.md",
        "tests/README.md",
    }
    for domain in manifest.domains:
        relative_paths.add(domain.authority)
        relative_paths.update(domain.thin_references)
        relative_paths.update(domain.exclusive_allowlist)
    docs_root = repo / "docs"
    if docs_root.is_dir():
        relative_paths.update(
            path.relative_to(repo).as_posix()
            for path in docs_root.glob("*.md")
            if path.is_file()
        )
    skills_root = repo / ".codex/skills"
    if skills_root.is_dir():
        relative_paths.update(
            path.relative_to(repo).as_posix()
            for path in skills_root.glob("*/SKILL.md")
            if path.is_file()
        )
    return tuple(repo / path for path in sorted(relative_paths))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--git-range")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = validate_repository(
            Path(args.repo_root).expanduser().resolve(),
            git_range=args.git_range,
        )
    except (OSError, RuntimeError) as exc:
        print(f"documentation authority error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
