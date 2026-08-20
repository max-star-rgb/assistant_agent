"""Deterministic dependency intent and strict Python wheel lock parsing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)

from assistant_agent.coding.config import CodingRepositoryConfig
from assistant_agent.coding.models import (
    CodingDependencyApprovalDecision,
    CodingDependencyManifest,
    CodingDependencyPlan,
    CodingDependencyWheel,
    CodingLockedDependency,
)


_HASH = re.compile(r"(?:^|\s)--hash=sha256:([0-9a-f]{64})(?=\s|$)")


def build_dependency_plan(
    repository: CodingRepositoryConfig,
    workspace_root: Path,
    *,
    changed_paths: tuple[str, ...],
) -> CodingDependencyPlan | None:
    profile = repository.dependency_profile
    if profile is None or profile.lockfile_path not in changed_paths:
        return None
    root = workspace_root.resolve()
    lockfile = root.joinpath(*profile.lockfile_path.split("/"))
    if lockfile.is_symlink() or not lockfile.is_file():
        raise ValueError("dependency_lockfile_invalid")
    try:
        if not lockfile.resolve().is_relative_to(root):
            raise ValueError("dependency_lockfile_invalid")
        raw = lockfile.read_bytes()
    except OSError as exc:
        raise ValueError("dependency_lockfile_invalid") from exc
    if len(raw) > profile.max_lockfile_bytes:
        raise ValueError("dependency_lockfile_invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("dependency_lockfile_invalid") from exc
    packages = _parse_lockfile(text, max_packages=profile.max_files)
    policy_payload = profile.model_dump(mode="json")
    policy_digest = _digest(policy_payload)
    values: dict[str, object] = {
        "profile_id": profile.profile_id,
        "ecosystem": profile.ecosystem,
        "lockfile_path": profile.lockfile_path,
        "lockfile_digest": hashlib.sha256(raw).hexdigest(),
        "packages": packages,
        "package_count": len(packages),
        "allowed_hosts": profile.allowed_hosts,
        "allowed_ports": profile.allowed_ports,
        "timeout_seconds": profile.timeout_seconds,
        "max_download_bytes": profile.max_download_bytes,
        "max_files": profile.max_files,
        "max_file_bytes": profile.max_file_bytes,
        "policy_digest": policy_digest,
    }
    return CodingDependencyPlan.model_validate(
        {**values, "plan_digest": _digest(values)}
    )


def dependency_interrupt_payload(plan: CodingDependencyPlan) -> dict[str, object]:
    return {
        "action": "coding_dependency_install",
        "profile_id": plan.profile_id,
        "ecosystem": plan.ecosystem,
        "lockfile_path": plan.lockfile_path,
        "lockfile_digest": plan.lockfile_digest,
        "package_count": plan.package_count,
        "allowed_hosts": list(plan.allowed_hosts),
        "allowed_ports": list(plan.allowed_ports),
        "timeout_seconds": plan.timeout_seconds,
        "max_download_bytes": plan.max_download_bytes,
        "max_files": plan.max_files,
        "max_file_bytes": plan.max_file_bytes,
        "policy_digest": plan.policy_digest,
        "plan_digest": plan.plan_digest,
    }


def validate_dependency_approval(
    plan: CodingDependencyPlan,
    raw: object,
) -> Literal["approve", "reject"]:
    try:
        decision = CodingDependencyApprovalDecision.model_validate(raw)
    except Exception as exc:
        raise ValueError("dependency_approval_mismatch") from exc
    if decision.decision == "approve" and decision.plan_digest != plan.plan_digest:
        raise ValueError("dependency_approval_mismatch")
    return decision.decision


def validate_wheelhouse(
    plan: CodingDependencyPlan,
    root: Path,
) -> CodingDependencyManifest:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("dependency_artifact_invalid")
    locked = {item.name: item for item in plan.packages}
    seen: set[str] = set()
    wheels: list[CodingDependencyWheel] = []
    total_bytes = 0
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError as exc:
        raise ValueError("dependency_artifact_invalid") from exc
    if not entries or len(entries) > plan.max_files:
        raise ValueError("dependency_artifact_invalid")
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("dependency_artifact_invalid") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not entry.name.endswith(".whl")
            or len(entry.name.encode("utf-8")) > 255
            or any(character in entry.name for character in ("\x00", "\n", "\r"))
            or metadata.st_size > plan.max_file_bytes
        ):
            raise ValueError("dependency_artifact_invalid")
        try:
            parsed_name, parsed_version, _, _ = parse_wheel_filename(entry.name)
        except InvalidWheelFilename as exc:
            raise ValueError("dependency_artifact_invalid") from exc
        name = canonicalize_name(parsed_name)
        version = str(parsed_version)
        expected = locked.get(name)
        if (
            expected is None
            or expected.version != version
            or name in seen
        ):
            raise ValueError("dependency_artifact_invalid")
        digest = _file_digest(Path(entry.path), plan.max_file_bytes)
        if digest not in expected.sha256:
            raise ValueError("dependency_artifact_invalid")
        seen.add(name)
        total_bytes += metadata.st_size
        if total_bytes > plan.max_download_bytes:
            raise ValueError("dependency_artifact_invalid")
        wheels.append(
            CodingDependencyWheel(
                filename=entry.name,
                name=name,
                version=version,
                sha256=digest,
                size_bytes=metadata.st_size,
            )
        )
    if seen != set(locked):
        raise ValueError("dependency_artifact_invalid")
    values: dict[str, object] = {
        "plan_digest": plan.plan_digest,
        "lockfile_digest": plan.lockfile_digest,
        "policy_digest": plan.policy_digest,
        "wheels": tuple(wheels),
        "total_bytes": total_bytes,
    }
    return CodingDependencyManifest.model_validate(
        {**values, "manifest_digest": _digest(values)}
    )


@contextmanager
def temporary_wheelhouse(parent: Path) -> Iterator[Path]:
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="dependency-wheelhouse-", dir=parent))
    root.chmod(0o700)
    try:
        yield root
    finally:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise ValueError("dependency_cleanup_failed") from exc


def _file_digest(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(65_536):
                total += len(chunk)
                if total > limit:
                    raise ValueError("dependency_artifact_invalid")
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("dependency_artifact_invalid") from exc
    return digest.hexdigest()


def _parse_lockfile(
    text: str,
    *,
    max_packages: int,
) -> tuple[CodingLockedDependency, ...]:
    logical_lines = _logical_lines(text)
    packages: list[CodingLockedDependency] = []
    names: set[str] = set()
    for line in logical_lines:
        hashes = tuple(sorted(set(_HASH.findall(line))))
        requirement_text = _HASH.sub("", line).strip()
        if not hashes or "--" in requirement_text:
            raise ValueError("dependency_lockfile_invalid")
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise ValueError("dependency_lockfile_invalid") from exc
        specifiers = tuple(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.marker is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError("dependency_lockfile_invalid")
        name = canonicalize_name(requirement.name)
        if name in names:
            raise ValueError("dependency_lockfile_invalid")
        names.add(name)
        packages.append(
            CodingLockedDependency(
                name=name,
                version=specifiers[0].version,
                sha256=hashes,
            )
        )
        if len(packages) > max_packages:
            raise ValueError("dependency_lockfile_invalid")
    if not packages:
        raise ValueError("dependency_lockfile_invalid")
    return tuple(sorted(packages, key=lambda item: item.name))


def _logical_lines(text: str) -> tuple[str, ...]:
    logical: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if pending:
                raise ValueError("dependency_lockfile_invalid")
            continue
        if "#" in line and "--hash=sha256:" not in line:
            raise ValueError("dependency_lockfile_invalid")
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    if pending:
        raise ValueError("dependency_lockfile_invalid")
    return tuple(logical)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.model_dump(mode="json"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "build_dependency_plan",
    "dependency_interrupt_payload",
    "temporary_wheelhouse",
    "validate_dependency_approval",
    "validate_wheelhouse",
]
