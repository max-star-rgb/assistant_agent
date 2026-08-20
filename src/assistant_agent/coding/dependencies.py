"""Deterministic dependency intent and strict Python wheel lock parsing."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from assistant_agent.coding.config import CodingRepositoryConfig
from assistant_agent.coding.models import (
    CodingDependencyApprovalDecision,
    CodingDependencyPlan,
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
    "validate_dependency_approval",
]
