"""Explicit, disabled-by-default configuration for AI coding workspaces."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
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
    commands: dict[str, CodingCommandConfig] = Field(default_factory=dict)
    verification_sequence: tuple[str, ...] = ()
    integration_enabled: bool = False
    sandbox_enabled: bool = False
    sandbox_image: str | None = Field(default=None, min_length=1, max_length=512)
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
        if re.fullmatch(
            r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}",
            value,
        ) is None:
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
    ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    max_patch_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    max_changed_files: int = Field(default=32, ge=1, le=256)
    max_file_bytes: int = Field(default=2_097_152, ge=1_024, le=10_485_760)

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


def _require_git_worktree(path: Path) -> None:
    if not path.is_dir():
        raise ValueError("coding repository path must exist")
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("coding repository Git validation failed") from exc
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise ValueError("coding repository path must be a Git worktree")


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


__all__ = ["CodingCommandConfig", "CodingConfig", "CodingRepositoryConfig"]
