"""Public Assistant configuration and private run facts."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
from typing import Any, Literal, Self

from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def studio_working_directory() -> Path:
    """Return the shared local Studio workspace."""

    return Path.home() / "assistant_cqy" / "studio"


def media_user_working_directory(user_id: str) -> Path:
    """Return a stable workspace without exposing the media user identifier."""

    identity = user_id.strip()
    if not identity:
        raise ValueError("media user identity is required")
    opaque_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return Path.home() / "assistant_cqy" / "media" / f"usr_{opaque_id}"


def ensure_working_directory(path: Path) -> Path:
    host_root = Path.home().resolve()
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(host_root):
        raise ValueError(f"cwd must be a directory under {host_root}")
    (resolved / "ASSISTANT.md").touch(exist_ok=True)
    for subdirectory in (".agent/browser", ".agent/downloads", "outputs"):
        (resolved / subdirectory).mkdir(parents=True, exist_ok=True)
    return resolved


class AuthenticatedUserRequired(PermissionError):
    """A production operation requires Agent Server authenticated identity."""


class AssistantRunContext(BaseModel):
    """User-facing Assistant configuration supplied through Runtime.context."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    persona: Literal["general", "call_assistant"] = Field(
        default="general",
        title="助理角色",
        description="general：通用助理；call_assistant：简洁口语化的通话助理。",
        json_schema_extra={"langgraph_nodes": ["model"]},
    )
    cwd: Path = Field(
        default_factory=studio_working_directory,
        description="本轮 Assistant 使用的默认工作目录。",
        json_schema_extra={
            "default": str(Path.home() / "assistant_cqy" / "studio")
        },
    )
    enable_memory: bool = Field(
        default=True,
        json_schema_extra={
            "langgraph_nodes": [
                "MemoryLifecycleMiddleware.before_agent",
                "MemoryLifecycleMiddleware.after_agent",
            ]
        },
    )
    device_id: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$",
        description="已在本机注册的执行设备；绑定后离线不回退服务端。",
    )
    require_tool_approval: bool = Field(
        default=False,
        description="工具执行前是否要求人工审批；本机开发默认关闭，可显式启用。",
    )
    context_compaction_trigger_tokens: int | None = Field(
        default=None,
        ge=1,
        title="上下文压缩触发 Token 数",
        description="触发上下文压缩的绝对 token 数；留空时使用服务端默认值。",
        json_schema_extra={"langgraph_nodes": ["model"]},
    )
    context_compaction_keep_tokens: int | None = Field(
        default=None,
        ge=1,
        title="压缩后保留 Token 数",
        description="压缩后保留的绝对 token 数；留空时使用服务端默认值。",
        json_schema_extra={"langgraph_nodes": ["model"]},
    )

    @field_validator("cwd", mode="before")
    @classmethod
    def validate_cwd(cls, value: object) -> Path:
        try:
            cwd = Path(os.path.normpath(Path(value).expanduser()))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("cwd must be an absolute path") from exc
        if not cwd.is_absolute():
            raise ValueError("cwd must be an absolute path")
        return cwd

    @model_validator(mode="after")
    def validate_context_compaction_limits(self) -> Self:
        if self.device_id is None and not self.cwd.is_relative_to(Path.home()):
            raise ValueError(f"cwd must be a directory under {Path.home()}")
        trigger = self.context_compaction_trigger_tokens
        keep = self.context_compaction_keep_tokens
        if (trigger is None) != (keep is None):
            raise ValueError("context compaction trigger and keep must be set together")
        if trigger is not None and keep is not None and keep >= trigger:
            raise ValueError("context compaction keep must be lower than trigger")
        return self


ASSISTANT_RUNTIME_METADATA_KEY = "assistant_agent_runtime"


class AssistantRuntimeFacts(BaseModel):
    """Server-issued run facts that must not become Assistant configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entry_profile: str = Field(default="agent_server", min_length=1, max_length=160)
    visual_capability_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )


def assistant_runtime_facts(config: Mapping[str, Any]) -> AssistantRuntimeFacts:
    """Read private runtime facts from namespaced RunnableConfig metadata."""

    metadata = config.get("metadata")
    payload = (
        metadata.get(ASSISTANT_RUNTIME_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(payload, Mapping):
        return AssistantRuntimeFacts()
    try:
        return AssistantRuntimeFacts.model_validate(dict(payload))
    except ValueError:
        return AssistantRuntimeFacts()


def assistant_runtime_metadata(
    facts: AssistantRuntimeFacts,
) -> dict[str, dict[str, object]]:
    """Build the namespaced metadata fragment used by trusted run adapters."""

    return {
        ASSISTANT_RUNTIME_METADATA_KEY: facts.model_dump(exclude_none=True),
    }


def authenticated_user_identity(runtime: Runtime[object]) -> str:
    """Read the sole user identity from LangGraph ServerInfo."""

    server_info = runtime.server_info
    user = server_info.user if server_info is not None else None
    identity = str(getattr(user, "identity", "")).strip()
    if not identity:
        raise AuthenticatedUserRequired(
            "Agent Server authenticated user identity is required."
        )
    return identity


__all__ = [
    "ASSISTANT_RUNTIME_METADATA_KEY",
    "AssistantRunContext",
    "AssistantRuntimeFacts",
    "AuthenticatedUserRequired",
    "assistant_runtime_facts",
    "assistant_runtime_metadata",
    "authenticated_user_identity",
    "ensure_working_directory",
    "media_user_working_directory",
    "studio_working_directory",
]
