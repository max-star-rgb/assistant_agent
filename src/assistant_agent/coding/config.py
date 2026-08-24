"""Explicit, disabled-by-default configuration for AI coding workspaces."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}

_DIGEST_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}"
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class CodingDependencyProfile(BaseModel):
    """Server-owned public Python dependency download policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    ecosystem: Literal["python-pip-wheel"]
    lockfile_path: str = Field(min_length=1, max_length=240)
    trigger: Literal["lockfile_changed"] = "lockfile_changed"
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_ports: tuple[int, ...] = (443,)
    downloader_image: str = Field(min_length=73, max_length=512)
    proxy_image: str = Field(min_length=73, max_length=512)
    credential_profile_id: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$",
    )
    timeout_seconds: int = Field(default=300, ge=10, le=1_800)
    max_download_bytes: int = Field(
        default=536_870_912,
        ge=1_048_576,
        le=4_294_967_296,
    )
    max_files: int = Field(default=512, ge=1, le=4_096)
    max_file_bytes: int = Field(
        default=134_217_728,
        ge=1_048_576,
        le=1_073_741_824,
    )
    max_lockfile_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)

    @field_validator("allowed_hosts", "allowed_ports", mode="before")
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("lockfile_path")
    @classmethod
    def _safe_lockfile_path(cls, value: str) -> str:
        if (
            "\\" in value
            or any(character in value for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("dependency lockfile path is invalid")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or str(path) != value
            or any(part in {"", ".", "..", ".git"} for part in path.parts)
        ):
            raise ValueError("dependency lockfile path is invalid")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _exact_public_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw_host in value:
            if not isinstance(raw_host, str):
                raise ValueError("dependency egress host is invalid")
            try:
                host = raw_host.encode("idna").decode("ascii").lower()
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("dependency egress host cannot be an IP literal")
            labels = host.split(".")
            if (
                raw_host != host
                or len(labels) < 2
                or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
            ):
                raise ValueError("dependency egress host must be an exact FQDN")
            normalized.append(host)
        if len(set(normalized)) != len(normalized):
            raise ValueError("dependency egress hosts cannot contain duplicates")
        return tuple(sorted(normalized))

    @field_validator("allowed_ports")
    @classmethod
    def _https_only(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != (443,):
            raise ValueError("dependency egress only supports HTTPS port 443")
        return value

    @field_validator("downloader_image", "proxy_image")
    @classmethod
    def _digest_pinned_image(cls, value: str) -> str:
        if _DIGEST_IMAGE.fullmatch(value) is None:
            raise ValueError("dependency image must be pinned by sha256 digest")
        return value


class CodingCredentialProfile(BaseModel):
    """Operator-owned metadata for one private registry credential."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    credential_profile_id: str = Field(
        pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$"
    )
    registry_host: str
    registry_base_path: str = Field(min_length=1, max_length=240)
    auth_scheme: Literal["bearer"] = "bearer"
    secret_env: str = Field(
        pattern=r"^MULTIMODAL_AGENT_CODING_CREDENTIAL_[A-Z0-9_]{1,96}$"
    )
    lease_ttl_seconds: int = Field(default=120, ge=30, le=900)
    gateway_image: str = Field(min_length=73, max_length=512)

    @field_validator("registry_host")
    @classmethod
    def _exact_registry_host(cls, value: str) -> str:
        try:
            host = value.encode("idna").decode("ascii").lower()
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("credential registry host cannot be an IP literal")
        labels = host.split(".")
        if (
            value != host
            or len(labels) < 2
            or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
        ):
            raise ValueError("credential registry host must be an exact FQDN")
        return host

    @field_validator("registry_base_path")
    @classmethod
    def _safe_registry_path(cls, value: str) -> str:
        if any(character in value for character in ("\\", "\x00", "\n", "\r", "?", "#")):
            raise ValueError("credential registry base path is invalid")
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or not value.startswith("/")
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise ValueError("credential registry base path is invalid")
        return value

    @field_validator("gateway_image")
    @classmethod
    def _digest_pinned_gateway(cls, value: str) -> str:
        if _DIGEST_IMAGE.fullmatch(value) is None:
            raise ValueError("credential gateway image must be pinned by sha256 digest")
        return value

class CodingArtifactExport(BaseModel):
    """One server-owned build output eligible for governed export."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    export_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    command_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    path: str = Field(min_length=1, max_length=240)
    media_type: str = Field(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$"
    )
    max_bytes: int = Field(ge=1_024, le=1_073_741_824)

    @field_validator("path")
    @classmethod
    def _safe_export_path(cls, value: str) -> str:
        return _safe_artifact_relative_path(value, "artifact export path")


class CodingArtifactProfile(BaseModel):
    """Server-owned ingress and build-output artifact policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    manifest_path: str = Field(min_length=1, max_length=240)
    trigger: Literal["manifest_changed"] = "manifest_changed"
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_ports: tuple[int, ...] = (443,)
    fetcher_image: str = Field(min_length=73, max_length=512)
    proxy_image: str = Field(min_length=73, max_length=512)
    scanner_image: str = Field(min_length=73, max_length=512)
    allowed_media_types: tuple[str, ...] = Field(min_length=1, max_length=32)
    exports: dict[str, CodingArtifactExport] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=10, le=1_800)
    max_artifacts: int = Field(default=32, ge=1, le=512)
    max_total_bytes: int = Field(default=536_870_912, ge=1_048_576, le=4_294_967_296)
    max_file_bytes: int = Field(default=134_217_728, ge=1_024, le=1_073_741_824)
    max_manifest_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    bundle_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)

    @field_validator(
        "allowed_hosts", "allowed_ports", "allowed_media_types", mode="before"
    )
    @classmethod
    def _tuple_values(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("manifest_path")
    @classmethod
    def _safe_manifest_path(cls, value: str) -> str:
        return _safe_artifact_relative_path(value, "artifact manifest path")

    @field_validator("allowed_hosts")
    @classmethod
    def _exact_artifact_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw_host in value:
            if not isinstance(raw_host, str):
                raise ValueError("artifact host is invalid")
            try:
                host = raw_host.encode("idna").decode("ascii").lower()
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("artifact host cannot be an IP literal")
            labels = host.split(".")
            if (
                raw_host != host
                or len(labels) < 2
                or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
            ):
                raise ValueError("artifact host must be an exact FQDN")
            normalized.append(host)
        if len(set(normalized)) != len(normalized):
            raise ValueError("artifact hosts cannot contain duplicates")
        return tuple(sorted(normalized))

    @field_validator("allowed_ports")
    @classmethod
    def _https_only(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != (443,):
            raise ValueError("artifact ingress only supports HTTPS port 443")
        return value

    @field_validator("fetcher_image", "proxy_image", "scanner_image")
    @classmethod
    def _digest_pinned_image(cls, value: str) -> str:
        if _DIGEST_IMAGE.fullmatch(value) is None:
            raise ValueError("artifact image must be pinned by sha256 digest")
        return value

    @field_validator("allowed_media_types")
    @classmethod
    def _safe_media_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pattern = re.compile(
            r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}"
        )
        if any(pattern.fullmatch(item) is None for item in value):
            raise ValueError("artifact media type is invalid")
        if len(set(value)) != len(value):
            raise ValueError("artifact media types cannot contain duplicates")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _validate_exports(self) -> "CodingArtifactProfile":
        for export_id, item in self.exports.items():
            if export_id != item.export_id:
                raise ValueError("artifact export key must match export_id")
            if item.media_type not in self.allowed_media_types:
                raise ValueError("artifact export media type is not allowed")
            if item.max_bytes > self.max_file_bytes:
                raise ValueError("artifact export exceeds profile file limit")
        paths = [item.path for item in self.exports.values()]
        if len(set(paths)) != len(paths):
            raise ValueError("artifact export paths cannot contain duplicates")
        return self


class CodingCommandConfig(BaseModel):
    """One server-owned command ID mapped to immutable process arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    kind: Literal["test", "lint", "format", "build"]
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=300, ge=1, le=1_800)
    cpu_seconds: int = Field(default=120, ge=1, le=1_800)
    cpu_cores: float = Field(default=1.0, ge=0.1, le=16.0)
    memory_bytes: int = Field(default=1_073_741_824, ge=67_108_864, le=17_179_869_184)
    max_processes: int = Field(default=64, ge=4, le=512)
    max_files: int = Field(default=100_000, ge=16, le=1_000_000)
    max_output_bytes: int = Field(default=1_048_576, ge=1_024, le=16_777_216)
    max_disk_bytes: int = Field(default=1_073_741_824, ge=1_048_576, le=17_179_869_184)

    @field_validator("argv", mode="before")
    @classmethod
    def _tuple_argv(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item or "\n" in item or "\r" in item for item in value):
            raise ValueError("coding command arguments must be non-empty single-line strings")
        executable = Path(value[0]).name.lower()
        if executable in _SHELL_EXECUTABLES:
            raise ValueError("coding commands cannot invoke a shell")
        return value


class CodingRepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repo_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    path: Path
    target_branch: str = Field(min_length=1, max_length=160)
    parallel_analysis_enabled: bool = False
    commands: dict[str, CodingCommandConfig] = Field(default_factory=dict)
    verification_sequence: tuple[str, ...] = ()
    integration_enabled: bool = False
    sandbox_enabled: bool = False
    sandbox_image: str | None = Field(default=None, min_length=1, max_length=512)
    dependency_profile: CodingDependencyProfile | None = None
    artifact_profile: CodingArtifactProfile | None = None
    commit_author_name: str = Field(default="Assistant Agent", min_length=1, max_length=160)
    commit_author_email: str = Field(
        default="assistant-agent@localhost",
        min_length=3,
        max_length=254,
    )

    @field_validator("verification_sequence", mode="before")
    @classmethod
    def _tuple_sequence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("target_branch")
    @classmethod
    def _validate_target_branch(cls, value: str) -> str:
        if value.startswith("-") or any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("coding target branch must be a literal branch name")
        try:
            completed = subprocess.run(
                ["git", "check-ref-format", "--branch", value],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                env=_governed_git_environment(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("coding target branch validation failed") from exc
        if completed.returncode != 0:
            raise ValueError("coding target branch must be a literal branch name")
        return value

    @field_validator("commit_author_name", "commit_author_email")
    @classmethod
    def _single_line_identity(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("coding commit identity must be a single line")
        return value

    @field_validator("sandbox_image")
    @classmethod
    def _digest_pinned_sandbox_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _DIGEST_IMAGE.fullmatch(value) is None:
            raise ValueError("coding sandbox image must be pinned by sha256 digest")
        return value

    @model_validator(mode="after")
    def _validate_commands(self) -> "CodingRepositoryConfig":
        for command_id, command in self.commands.items():
            if command_id != command.command_id:
                raise ValueError("coding command key must match command_id")
        if len(set(self.verification_sequence)) != len(self.verification_sequence):
            raise ValueError("coding verification sequence cannot contain duplicates")
        missing = set(self.verification_sequence).difference(self.commands)
        if missing:
            raise ValueError("coding verification sequence references an unknown command")
        if self.integration_enabled and not self.verification_sequence:
            raise ValueError("coding integration requires a verification sequence")
        if self.sandbox_enabled and not self.verification_sequence:
            raise ValueError("coding sandbox requires a verification sequence")
        if self.sandbox_enabled and self.sandbox_image is None:
            raise ValueError("coding sandbox requires a digest-pinned image")
        if self.dependency_profile is not None and not self.sandbox_enabled:
            raise ValueError("coding dependencies require the Stage 4A sandbox")
        if self.artifact_profile is not None:
            if not self.sandbox_enabled:
                raise ValueError("coding artifacts require the Stage 4A sandbox")
            missing_commands = {
                item.command_id for item in self.artifact_profile.exports.values()
            }.difference(self.commands)
            if missing_commands:
                raise ValueError("artifact export references an unknown command")
            if any(
                self.commands[item.command_id].kind != "build"
                for item in self.artifact_profile.exports.values()
            ):
                raise ValueError("artifact export requires a build command")
        return self


class CodingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False
    workspace_root: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir())
        / "assistant_agent"
        / "coding_workspaces"
    )
    repositories: dict[str, CodingRepositoryConfig] = Field(default_factory=dict)
    credential_profiles: dict[str, CodingCredentialProfile] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    max_patch_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    max_changed_files: int = Field(default=32, ge=1, le=256)
    max_file_bytes: int = Field(default=2_097_152, ge=1_024, le=10_485_760)
    analysis_snapshot_max_files: int = Field(default=4_096, ge=1, le=100_000)
    analysis_snapshot_max_total_bytes: int = Field(
        default=67_108_864,
        ge=1_024,
        le=1_073_741_824,
    )
    analysis_snapshot_max_scan_entries: int = Field(
        default=100_000,
        ge=1,
        le=1_000_000,
    )
    analysis_snapshot_max_scan_directories: int = Field(
        default=20_000,
        ge=1,
        le=200_000,
    )
    analysis_snapshot_max_scan_bytes: int = Field(
        default=268_435_456,
        ge=1,
        le=4_294_967_296,
    )
    analysis_snapshot_max_status_entries: int = Field(
        default=4_096,
        ge=1,
        le=100_000,
    )
    analysis_snapshot_max_status_bytes: int = Field(
        default=262_144,
        ge=64,
        le=4_194_304,
    )
    analysis_snapshot_max_diff_bytes: int = Field(
        default=262_144,
        ge=64,
        le=4_194_304,
    )

    @model_validator(mode="after")
    def _validate_boundaries(self) -> "CodingConfig":
        if not self.workspace_root.is_absolute():
            raise ValueError("coding workspace root must be absolute")
        if self.enabled and not self.repositories:
            raise ValueError("coding repository allowlist is required when enabled")
        workspace_root = self.workspace_root.resolve()
        for repo_id, repository in self.repositories.items():
            if repo_id != repository.repo_id:
                raise ValueError("coding repository key must match repo_id")
            if not repository.path.is_absolute():
                raise ValueError("coding repository paths must be absolute")
            repo_path = repository.path.resolve()
            if workspace_root == repo_path or workspace_root.is_relative_to(repo_path):
                raise ValueError("coding workspace root must be outside source repositories")
            dependency = repository.dependency_profile
            credential_id = dependency.credential_profile_id if dependency else None
            if credential_id is not None:
                credential = self.credential_profiles.get(credential_id)
                if credential is None:
                    raise ValueError("coding credential profile is not configured")
                if credential.registry_host not in dependency.allowed_hosts:
                    raise ValueError("coding credential registry host is not allowed")
        for profile_id, profile in self.credential_profiles.items():
            if profile_id != profile.credential_profile_id:
                raise ValueError("coding credential profile key must match profile id")
        return self

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "CodingConfig":
        source = os.environ if env is None else env
        repositories = _repositories(
            source.get("MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON", "{}")
        )
        credential_profiles = _credential_profiles(
            source.get("MULTIMODAL_AGENT_CODING_CREDENTIAL_PROFILES_JSON", "{}")
        )
        workspace_value = source.get("MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT", "")
        workspace_root = (
            Path(workspace_value).expanduser().resolve()
            if workspace_value.strip()
            else Path(tempfile.gettempdir()).resolve()
            / "assistant_agent"
            / "coding_workspaces"
        )
        return cls(
            enabled=_bool_value(
                source.get("MULTIMODAL_AGENT_CODING_ENABLED", "false")
            ),
            workspace_root=workspace_root,
            repositories=repositories,
            credential_profiles=credential_profiles,
            ttl_seconds=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_TTL_SECONDS",
                86_400,
            ),
            max_patch_bytes=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_MAX_PATCH_BYTES",
                262_144,
            ),
            max_changed_files=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_MAX_CHANGED_FILES",
                32,
            ),
            max_file_bytes=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_MAX_FILE_BYTES",
                2_097_152,
            ),
            analysis_snapshot_max_files=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_FILES",
                4_096,
            ),
            analysis_snapshot_max_total_bytes=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_TOTAL_BYTES",
                67_108_864,
            ),
            analysis_snapshot_max_scan_entries=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_SCAN_ENTRIES",
                100_000,
            ),
            analysis_snapshot_max_scan_directories=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_SCAN_DIRECTORIES",
                20_000,
            ),
            analysis_snapshot_max_scan_bytes=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_SCAN_BYTES",
                268_435_456,
            ),
            analysis_snapshot_max_status_entries=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_STATUS_ENTRIES",
                4_096,
            ),
            analysis_snapshot_max_status_bytes=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_STATUS_BYTES",
                262_144,
            ),
            analysis_snapshot_max_diff_bytes=_int_value(
                source,
                "MULTIMODAL_AGENT_CODING_ANALYSIS_SNAPSHOT_MAX_DIFF_BYTES",
                262_144,
            ),
        )


def _repositories(raw: str) -> dict[str, CodingRepositoryConfig]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("coding repository allowlist must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("coding repository allowlist must be a JSON object")
    repositories: dict[str, CodingRepositoryConfig] = {}
    for repo_id, value in parsed.items():
        if not isinstance(repo_id, str) or not isinstance(value, dict):
            raise ValueError("coding repository entries must be objects")
        item: dict[str, Any] = dict(value)
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise ValueError("coding repository paths must be absolute")
        path = Path(raw_path).expanduser().resolve()
        _require_git_worktree(path)
        item.update(repo_id=repo_id, path=path)
        repositories[repo_id] = CodingRepositoryConfig.model_validate(item)
    return repositories


def _credential_profiles(raw: str) -> dict[str, CodingCredentialProfile]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("coding credential profiles must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("coding credential profiles must be a JSON object")
    profiles: dict[str, CodingCredentialProfile] = {}
    for profile_id, value in parsed.items():
        if not isinstance(profile_id, str) or not isinstance(value, dict):
            raise ValueError("coding credential profile entries must be objects")
        item = dict(value)
        item["credential_profile_id"] = profile_id
        profiles[profile_id] = CodingCredentialProfile.model_validate(item)
    return profiles


def _require_git_worktree(path: Path) -> None:
    if not path.is_dir():
        raise ValueError("coding repository path must exist")
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_governed_git_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("coding repository Git validation failed") from exc
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise ValueError("coding repository path must be a Git worktree")


def _safe_artifact_relative_path(value: str, label: str) -> str:
    if "\\" in value or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _bool_value(raw: str) -> bool:
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("coding enabled flag must be boolean")


def _int_value(source: Mapping[str, str], name: str, default: int) -> int:
    raw = str(source.get(name, default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _governed_git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }


__all__ = [
    "CodingCommandConfig",
    "CodingArtifactExport",
    "CodingArtifactProfile",
    "CodingConfig",
    "CodingCredentialProfile",
    "CodingDependencyProfile",
    "CodingRepositoryConfig",
]
