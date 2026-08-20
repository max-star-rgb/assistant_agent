"""Deterministic contracts for governed coding artifact ingress."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from assistant_agent.coding.config import CodingRepositoryConfig
from assistant_agent.coding.models import (
    CodingArtifactApprovalDecision,
    CodingArtifactDescriptor,
    CodingArtifactIngressPlan,
    CodingArtifactIngressManifest,
    CodingScannedArtifact,
)


class _ArtifactLock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["coding_artifacts_v1"]
    artifacts: tuple[CodingArtifactDescriptor, ...] = Field(min_length=1, max_length=512)

    @field_validator("artifacts", mode="before")
    @classmethod
    def _tuple_artifacts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()


def build_artifact_ingress_plan(
    repository: CodingRepositoryConfig,
    workspace_root: Path,
    *,
    changed_paths: tuple[str, ...],
) -> CodingArtifactIngressPlan | None:
    profile = repository.artifact_profile
    if profile is None or profile.manifest_path not in changed_paths:
        return None
    root = workspace_root.resolve()
    manifest = root.joinpath(*profile.manifest_path.split("/"))
    try:
        if (
            manifest.is_symlink()
            or not manifest.is_file()
            or not manifest.resolve().is_relative_to(root)
        ):
            raise ValueError("artifact_manifest_invalid")
        raw = manifest.read_bytes()
    except OSError as exc:
        raise ValueError("artifact_manifest_invalid") from exc
    if len(raw) > profile.max_manifest_bytes:
        raise ValueError("artifact_manifest_invalid")
    try:
        parsed = json.loads(raw)
        locked = _ArtifactLock.model_validate(parsed)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
        raise ValueError("artifact_manifest_invalid") from exc
    if len(locked.artifacts) > profile.max_artifacts:
        raise ValueError("artifact_manifest_invalid")
    artifact_ids: set[str] = set()
    filenames: set[str] = set()
    total = 0
    normalized: list[CodingArtifactDescriptor] = []
    for item in locked.artifacts:
        if (
            item.artifact_id in artifact_ids
            or item.filename in filenames
            or item.media_type not in profile.allowed_media_types
            or item.size_bytes > profile.max_file_bytes
        ):
            raise ValueError("artifact_manifest_invalid")
        _validate_url(item.url, profile.allowed_hosts)
        path = PurePosixPath(item.filename)
        if (
            path.name != item.filename
            or item.filename in {".", ".."}
            or any(character in item.filename for character in ("\\", "\x00", "\n", "\r"))
        ):
            raise ValueError("artifact_manifest_invalid")
        artifact_ids.add(item.artifact_id)
        filenames.add(item.filename)
        total += item.size_bytes
        if total > profile.max_total_bytes:
            raise ValueError("artifact_manifest_invalid")
        normalized.append(item)
    normalized.sort(key=lambda item: item.artifact_id)
    policy_digest = _digest(profile.model_dump(mode="json"))
    values: dict[str, object] = {
        "profile_id": profile.profile_id,
        "manifest_path": profile.manifest_path,
        "manifest_digest": hashlib.sha256(raw).hexdigest(),
        "artifacts": tuple(normalized),
        "artifact_count": len(normalized),
        "allowed_hosts": profile.allowed_hosts,
        "allowed_ports": profile.allowed_ports,
        "timeout_seconds": profile.timeout_seconds,
        "max_total_bytes": profile.max_total_bytes,
        "max_file_bytes": profile.max_file_bytes,
        "policy_digest": policy_digest,
    }
    digest_values = {
        **values,
        "artifacts": [item.model_dump(mode="json") for item in normalized],
    }
    return CodingArtifactIngressPlan.model_validate(
        {**values, "plan_digest": _digest(digest_values)}
    )


def _validate_url(url: str, allowed_hosts: tuple[str, ...]) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("artifact_manifest_invalid") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("artifact_manifest_invalid")
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or not host
        or host != host.lower()
        or host not in allowed_hosts
        or parsed.netloc not in {host, f"{host}:443"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or any(part in {".", ".."} for part in PurePosixPath(decoded_path).parts)
        or any(character in url for character in ("\x00", "\n", "\r", "\\"))
    ):
        raise ValueError("artifact_manifest_invalid")


def artifact_interrupt_payload(plan: CodingArtifactIngressPlan) -> dict[str, object]:
    return {
        "action": "coding_artifact_ingress",
        "profile_id": plan.profile_id,
        "manifest_path": plan.manifest_path,
        "manifest_digest": plan.manifest_digest,
        "artifact_count": plan.artifact_count,
        "allowed_hosts": list(plan.allowed_hosts),
        "allowed_ports": list(plan.allowed_ports),
        "timeout_seconds": plan.timeout_seconds,
        "max_total_bytes": plan.max_total_bytes,
        "max_file_bytes": plan.max_file_bytes,
        "policy_digest": plan.policy_digest,
        "plan_digest": plan.plan_digest,
    }


def validate_artifact_approval(
    plan: CodingArtifactIngressPlan,
    raw: object,
) -> Literal["approve", "reject"]:
    try:
        decision = CodingArtifactApprovalDecision.model_validate(raw)
    except Exception as exc:
        raise ValueError("artifact_approval_mismatch") from exc
    if decision.decision == "approve" and decision.plan_digest != plan.plan_digest:
        raise ValueError("artifact_approval_mismatch")
    return decision.decision


def validate_artifact_bundle(
    plan: CodingArtifactIngressPlan,
    root: Path,
    *,
    scanner_policy_digest: str,
) -> CodingArtifactIngressManifest:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("artifact_fetch_failed")
    expected = {item.filename: item for item in plan.artifacts}
    scanned: list[CodingScannedArtifact] = []
    total = 0
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError as exc:
        raise ValueError("artifact_fetch_failed") from exc
    if len(entries) != len(expected):
        raise ValueError("artifact_fetch_failed")
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("artifact_fetch_failed") from exc
        descriptor = expected.get(entry.name)
        if (
            descriptor is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != descriptor.size_bytes
            or metadata.st_size > plan.max_file_bytes
        ):
            raise ValueError("artifact_fetch_failed")
        digest = _file_digest(Path(entry.path), plan.max_file_bytes)
        if digest != descriptor.sha256:
            raise ValueError("artifact_fetch_failed")
        total += metadata.st_size
        if total > plan.max_total_bytes:
            raise ValueError("artifact_fetch_failed")
        scanned.append(
            CodingScannedArtifact(
                artifact_id=descriptor.artifact_id,
                filename=descriptor.filename,
                media_type=descriptor.media_type,
                size_bytes=metadata.st_size,
                sha256=digest,
            )
        )
    values: dict[str, object] = {
        "plan_digest": plan.plan_digest,
        "policy_digest": plan.policy_digest,
        "scanner_policy_digest": scanner_policy_digest,
        "artifacts": tuple(sorted(scanned, key=lambda item: item.artifact_id)),
        "total_bytes": total,
    }
    digest_values = {
        **values,
        "artifacts": [
            item.model_dump(mode="json") for item in values["artifacts"]
        ],
    }
    return CodingArtifactIngressManifest.model_validate(
        {**values, "manifest_digest": _digest(digest_values)}
    )


def _file_digest(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(65_536):
                total += len(chunk)
                if total > limit:
                    raise ValueError("artifact_fetch_failed")
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("artifact_fetch_failed") from exc
    return digest.hexdigest()


__all__ = [
    "artifact_interrupt_payload",
    "build_artifact_ingress_plan",
    "validate_artifact_bundle",
    "validate_artifact_approval",
]
