#!/usr/bin/env python3
"""Collect deterministic, read-only evidence for repository documentation audits."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
PATH_RE = re.compile(
    r"(?:^|(?<=\s)|(?<=[(]))"
    r"((?:\.?\.?(?:/)|/)?(?:\.codex|docs|src|tests|scripts|config|configs|plans|haodanku-openapi-docs)/[^\s,;()]+|AGENTS\.md|README\.md)"
)
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "ftp", "data"}
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


class EvidenceError(RuntimeError):
    """An expected command-line validation failure."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise EvidenceError(result.stderr.strip() or "Git command failed")
    return result


def _validate_repository(path: str) -> Path:
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise EvidenceError(f"not a Git repository: {repo}")
    result = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise EvidenceError(f"not a Git repository: {repo}")
    top_level = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top_level).resolve() != repo:
        raise EvidenceError(f"repository root must be the Git top-level: {repo}")
    return repo


def _is_within_repository(repo: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(repo)
    except ValueError:
        return False
    return True


def _markdown_lines(path: Path):
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            suffix = fence.group(2)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
                continue
            if (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not suffix.strip()
            ):
                fence_character = None
                fence_length = 0
                continue
        if fence_character is None:
            yield line_number, line


def _documentation_paths(repo: Path) -> list[Path]:
    paths: set[Path] = set()
    for name in ("README.md", "AGENTS.md"):
        candidate = repo / name
        if candidate.is_file() and _is_within_repository(repo, candidate):
            paths.add(candidate)
    for directory in (
        repo / "docs",
        repo / ".codex" / "skills",
        repo / "haodanku-openapi-docs",
    ):
        if directory.is_dir():
            paths.update(
                path
                for path in directory.rglob("*.md")
                if path.is_file() and _is_within_repository(repo, path)
            )
    return sorted(paths, key=lambda path: path.relative_to(repo).as_posix())


def _location(path: str) -> str:
    if path in {"README.md", "AGENTS.md"}:
        return "repository_entry"
    if path.startswith(".codex/skills/"):
        return "project_skill"
    if path.startswith("docs/interview/"):
        return "interview"
    if path.startswith("docs/superpowers/") or path.startswith("plans/"):
        return "development_artifact"
    if path.startswith("docs/development/"):
        return "development"
    if path.startswith("docs/"):
        return "docs"
    return "other"


def _last_touch(repo: Path, relative_path: str) -> str | None:
    result = _git(repo, "log", "-1", "--format=%H", "--", relative_path, check=False)
    commit = result.stdout.strip()
    return commit or None


def _slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", text).strip("-")


def _anchors(path: Path, cache: dict[Path, set[str]]) -> set[str]:
    if path in cache:
        return cache[path]
    anchors: set[str] = set()
    counts: defaultdict[str, int] = defaultdict(int)
    try:
        for _, line in _markdown_lines(path):
            match = HEADING_RE.match(line)
            if not match:
                continue
            base = _slug(match.group(1))
            if not base:
                continue
            count = counts[base]
            anchors.add(base if count == 0 else f"{base}-{count}")
            counts[base] += 1
    except OSError:
        pass
    cache[path] = anchors
    return anchors


def _link_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")]
    return target.split(maxsplit=1)[0]


def _resolve_path(repo: Path, source: Path, target_path: str) -> tuple[Path | None, str | None]:
    decoded = unquote(target_path)
    if not decoded:
        return source, source.relative_to(repo).as_posix()
    candidate = repo / decoded.lstrip("/") if decoded.startswith("/") else source.parent / decoded
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(repo).as_posix()
    except ValueError:
        return None, None
    return resolved, relative


def _collect_markdown_links(
    repo: Path, documents: list[Path]
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    checks: list[dict[str, object]] = []
    inbound: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    anchor_cache: dict[Path, set[str]] = {}
    for source in documents:
        source_rel = source.relative_to(repo).as_posix()
        for line_number, line in _markdown_lines(source):
            for match in MARKDOWN_LINK_RE.finditer(line):
                target = _link_target(match.group(1))
                parsed = urlsplit(target)
                item: dict[str, object] = {
                    "source": source_rel,
                    "line": line_number,
                    "target": target,
                }
                if parsed.scheme or target.startswith("//"):
                    item.update({"status": "external", "resolved_path": None})
                    checks.append(item)
                    continue
                target_file, resolved_rel = _resolve_path(repo, source, parsed.path)
                item["resolved_path"] = resolved_rel
                if target_file is None or not target_file.exists():
                    item["status"] = "missing"
                elif parsed.fragment and unquote(parsed.fragment).lower() not in _anchors(
                    target_file, anchor_cache
                ):
                    item["status"] = "missing_anchor"
                else:
                    item["status"] = "ok"
                checks.append(item)
                if resolved_rel is not None:
                    inbound[resolved_rel].append(
                        {
                            "source": source_rel,
                            "line": line_number,
                            "target": target,
                            "kind": "markdown_link",
                        }
                    )
    checks.sort(key=lambda item: (str(item["source"]), int(item["line"]), str(item["target"])))
    for references in inbound.values():
        references.sort(key=lambda item: (str(item["source"]), int(item["line"]), str(item["target"])))
    return checks, dict(inbound)


def _collect_repository_paths(
    repo: Path, documents: list[Path]
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    checks: list[dict[str, object]] = []
    inbound: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for source in documents:
        source_rel = source.relative_to(repo).as_posix()
        for line_number, line in _markdown_lines(source):
            for code in INLINE_CODE_RE.findall(line):
                for match in PATH_RE.finditer(code):
                    target = match.group(1).rstrip(".:'\"")
                    item: dict[str, object] = {
                        "source": source_rel,
                        "line": line_number,
                        "target": target,
                        "resolved_path": None,
                    }
                    if any(character in target for character in "*?["):
                        item["status"] = "skipped_glob"
                    else:
                        resolved, relative = _resolve_path(repo, source, target)
                        # Inline repository paths are conventionally rooted at the repository.
                        rooted = (repo / target.lstrip("/")).resolve()
                        if not target.startswith(("./", "../", "/")):
                            try:
                                relative = rooted.relative_to(repo).as_posix()
                            except ValueError:
                                resolved, relative = None, None
                            else:
                                resolved = rooted
                        item["resolved_path"] = relative
                        if resolved is None:
                            item["status"] = "outside_repository"
                        else:
                            item["status"] = "ok" if resolved.exists() else "missing"
                    checks.append(item)
                    if item["resolved_path"] is not None:
                        inbound[str(item["resolved_path"])].append(
                            {
                                "source": source_rel,
                                "line": line_number,
                                "target": target,
                                "kind": "repository_path",
                            }
                        )
    checks.sort(key=lambda item: (str(item["source"]), int(item["line"]), str(item["target"])))
    for references in inbound.values():
        references.sort(key=lambda item: (str(item["source"]), int(item["line"]), str(item["target"])))
    return checks, dict(inbound)


def _git_changes(repo: Path, git_range: str | None) -> dict[str, object]:
    changes: dict[str, object] = {
        "requested": git_range is not None,
        "added": [],
        "modified": [],
        "deleted": [],
        "renamed": [],
    }
    if git_range is None:
        return changes
    validation = _git(
        repo, "rev-list", "--max-count=1", "--end-of-options", git_range, check=False
    )
    if validation.returncode != 0:
        detail = validation.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise EvidenceError(f"invalid Git range {git_range!r}{suffix}")
    result = _git(
        repo,
        "diff",
        "--name-status",
        "--find-renames",
        "--end-of-options",
        git_range,
        "--",
    )
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        status = fields[0]
        if status == "A":
            changes["added"].append(fields[1])
        elif status == "M":
            changes["modified"].append(fields[1])
        elif status == "D":
            changes["deleted"].append(fields[1])
        elif status.startswith("R"):
            changes["renamed"].append({"from": fields[1], "to": fields[2]})
        else:
            # Treat other content changes (copies, type changes, conflicts) as modifications.
            changes["modified"].append(fields[-1])
    for key in ("added", "modified", "deleted"):
        changes[key].sort()
    changes["renamed"].sort(key=lambda item: (item["from"], item["to"]))
    return changes


def collect(repo: Path, git_range: str | None) -> dict[str, object]:
    documents = _documentation_paths(repo)
    markdown_links, inbound = _collect_markdown_links(repo, documents)
    repository_paths, path_inbound = _collect_repository_paths(repo, documents)
    for target, references in path_inbound.items():
        inbound.setdefault(target, []).extend(references)
        inbound[target].sort(
            key=lambda item: (
                str(item["source"]),
                int(item["line"]),
                str(item["target"]),
                str(item["kind"]),
            )
        )
    inventory = []
    for path in documents:
        relative = path.relative_to(repo).as_posix()
        inventory.append(
            {
                "path": relative,
                "location": _location(relative),
                "last_touch_commit": _last_touch(repo, relative),
                "inbound_references": inbound.get(relative, []),
            }
        )
    return {
        "schema_version": 1,
        "repository_root": str(repo),
        "git_range": git_range,
        "documents": inventory,
        "git_changes": _git_changes(repo, git_range),
        "checks": {
            "markdown_links": markdown_links,
            "repository_paths": repository_paths,
        },
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, help="path to the Git repository root")
    parser.add_argument("--git-range", help="optional revision range, for example BASE..HEAD")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        repo = _validate_repository(args.repo_root)
        payload = collect(repo, args.git_range)
    except EvidenceError as exc:
        print(f"documentation evidence error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
