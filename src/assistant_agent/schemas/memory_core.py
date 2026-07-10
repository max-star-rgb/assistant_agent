"""Prompt-safe memory core observability contracts."""

from typing import Literal

from pydantic import BaseModel, Field


MemoryCoreMode = Literal["local_only", "dual_core", "remote_service"]


class MemoryCoreStatus(BaseModel):
    """Prompt-safe status for the active built-in/external memory cores."""

    mode: MemoryCoreMode
    memory_backend: str
    memory_local_backend: str
    active_store: str
    local_core: str | None = None
    local_store: str | None = None
    external_core: str | None = None
    external_core_configured: bool = False
    external_lifecycle_owner: bool = False
    remote_query_enabled: bool = False
    remote_query_degraded: bool = False
    remote_status: str = "not_applicable"
    remote_error_codes: list[str] = Field(default_factory=list)
